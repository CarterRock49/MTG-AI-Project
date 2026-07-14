import os
import json
import hashlib
import re
import platform
import subprocess
import multiprocessing
import threading
import torch
import time
import random
import logging
import argparse
import numpy as np
import traceback
import warnings
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from typing import Any, Dict, List, Type, Union, Optional
import sys

# Stable Baselines and Contrib Imports
from sb3_contrib.ppo_mask import MaskablePPO
import sb3_contrib.common.maskable.policies
from stable_baselines3.common.callbacks import (
    CheckpointCallback, 
    ProgressBarCallback,
    BaseCallback
)
from stable_baselines3.common.vec_env import (
    DummyVecEnv, SubprocVecEnv, VecEnvWrapper, VecMonitor)
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor
from stable_baselines3.common.env_util import make_vec_env
from stable_baselines3.common.utils import set_random_seed
from sb3_contrib.common.maskable.callbacks import MaskableEvalCallback
from sb3_contrib.common.maskable.evaluation import evaluate_policy
from sb3_contrib.common.maskable.utils import get_action_masks
from sb3_contrib.common.wrappers import ActionMasker
# Optuna for Hyperparameter Optimization
import optuna
# Additional imports for network functionality
import torch.nn.functional as F
import torch.nn as nn

# Import MTG Environment Components
from Playersim.card import Card, load_decks_and_card_db
from Playersim.environment import AlphaZeroMTGEnv
from Playersim.debug import DEBUG_MODE

# Custom Feature Extractor and Policy
class FixedWindowMTGExtractor(BaseFeaturesExtractor):
    """
    Features extractor that doesn't rely on CombinedExtractor.
    This provides full control over dimensions and network architecture.
    """
    def __init__(self, observation_space, features_dim=512):
        super().__init__(observation_space, features_dim=features_dim)
        
        self.output_dim = features_dim
        self.has_initialized = False
        
        # BUGFIX: was a plain dict, so PyTorch never registered these sub-networks.
        # Their weights were invisible to the optimizer (never trained), missing from
        # state_dict (never saved in checkpoints), and never moved to GPU.
        self.extractors = torch.nn.ModuleDict()
        
        # BUGFIX: was Embedding(10, 16), but engine phase indices go up to 19
        # (PHASE_CHOOSE) -> IndexError the first time an episode reached phase >= 10.
        # Size from the declared observation space instead.
        phase_dim = 16
        num_phases = 32  # fallback if the space is missing or unbounded
        merged_dim = 0
        if "phase" in observation_space.spaces:
            phase_space = observation_space.spaces["phase"]
            high = np.asarray(phase_space.high)
            if np.all(np.isfinite(high)):
                num_phases = int(high.max()) + 1
            merged_dim += int(np.prod(phase_space.shape)) * phase_dim
        self.phase_embedding = torch.nn.Embedding(num_phases, phase_dim)
        
        # Final projections
        self.preprocessing_dim = 256  # Intermediate dimension
        self.final_projection = torch.nn.Sequential(
            torch.nn.Linear(self.preprocessing_dim, self.output_dim),
            torch.nn.ReLU()
        )
        
        # Process each observation type separately
        for key, subspace in observation_space.spaces.items():
            if key == "phase" or key == "action_mask":
                continue
                
            if len(subspace.shape) == 1:
                # 1D vector observations (counts, flags, etc.)
                n_input = int(np.prod(subspace.shape))
                self.extractors[key] = torch.nn.Sequential(
                    torch.nn.Linear(n_input, 32),
                    torch.nn.ReLU(),
                    torch.nn.Linear(32, 64)
                )
                merged_dim += 64
            
            elif len(subspace.shape) == 2:
                # 2D observations like battlefield and hand
                n_cards, card_dim = subspace.shape
                self.extractors[key] = torch.nn.Sequential(
                    torch.nn.Linear(card_dim, 128),
                    torch.nn.ReLU(),
                    torch.nn.Linear(128, 64),
                    torch.nn.ReLU()
                )
                merged_dim += n_cards * 64
        
        # BUGFIX: feature_merger used to be created lazily inside forward(), AFTER the
        # optimizer had already collected parameters -> it was never trained, and a
        # freshly constructed policy had no such key -> MaskablePPO.load() mismatched.
        # The input width is now computed above, so it can be built here.
        self.feature_merger = torch.nn.Linear(merged_dim, self.preprocessing_dim)
        
        # Length-one gated feature transform. Hidden state is deliberately not
        # carried between policy calls, so this is not presented as recurrent.
        # Keep the ``lstm`` attribute name for checkpoint state_dict stability.
        self.lstm = torch.nn.LSTM(
            input_size=self.output_dim,
            hidden_size=self.output_dim,
            batch_first=True
        )
    
    @staticmethod
    def _symlog(tensor):
        """Compress unbounded observation scalars before any linear layer.

        Several declared observation bounds are saturation points, not
        typical magnitudes (P/T and combat damage saturate at 1e6, card ids
        reach 2**31). Feeding those raw into ``Linear`` layers let a single
        degenerate game drive value predictions to ~1e5 and poison the GAE
        targets for the whole batch. sign(x)*log1p(|x|) is monotone, exact
        near zero, and keeps every input within ~22.
        """
        return torch.sign(tensor) * torch.log1p(torch.abs(tensor))

    def forward(self, observations):
        """Process the observations through the feature extractors"""
        encoded_tensor_list = []

        # Process discrete observations
        if "phase" in observations:
            # Defensive clamp: never index outside the embedding table.
            phase_tensor = observations["phase"].long().clamp(
                0, self.phase_embedding.num_embeddings - 1)
            phase_emb = self.phase_embedding(phase_tensor)
            encoded_tensor_list.append(phase_emb)

        # Process continuous observation spaces
        for key, extractor in self.extractors.items():
            if key in observations:
                encoded_tensor_list.append(
                    extractor(self._symlog(observations[key])))
        
        batch_size = encoded_tensor_list[0].shape[0]
        
        # Merge features
        preprocessed_features = torch.cat([tensor.view(batch_size, -1) for tensor in encoded_tensor_list], dim=1)
        
        # feature_merger is now built in __init__ with a precomputed input width.
        assert preprocessed_features.shape[1] == self.feature_merger.in_features, (
            f"Feature width mismatch: got {preprocessed_features.shape[1]}, "
            f"extractor was built for {self.feature_merger.in_features}. "
            f"The observation space likely changed after construction.")
        merged_features = self.feature_merger(preprocessed_features)
        projected_features = self.final_projection(merged_features)
        
        # Apply the length-one gated feature transform.
        sequence = projected_features.unsqueeze(1)
        hidden_state = (
            torch.zeros(1, batch_size, self.output_dim, device=projected_features.device),
            torch.zeros(1, batch_size, self.output_dim, device=projected_features.device)
        )
        
        lstm_out, _ = self.lstm(sequence, hidden_state)
        lstm_features = lstm_out.squeeze(1)
        
        # Combine with residual connection
        result = projected_features + lstm_features
        
        return result


# Backward-compatible import path for checkpoints/configurations serialized
# before the active extractor received an honest non-recurrent name.
CompletelyFixedMTGExtractor = FixedWindowMTGExtractor

class FixedDimensionMaskableActorCriticPolicy(sb3_contrib.common.maskable.policies.MaskableActorCriticPolicy):
    """
    Custom policy that ensures dimensions match correctly between feature extractor and policy networks.
    """
    def _build_mlp_extractor(self) -> None:
        """
        Create the policy and value networks.
        """
        # Directly access the output_dim from the features extractor
        feature_dim = self.features_extractor.output_dim
        
        # Create MLP extractor with correct dimensions
        self.mlp_extractor = CustomMTGPolicyMLP(
            feature_dim=feature_dim,
            net_arch=self.net_arch,
            activation_fn=self.activation_fn
        )
        
        # Attach to device
        self.mlp_extractor.to(self.device)

class CustomMTGPolicyMLP(torch.nn.Module):
    """
    Custom MLP for policy and value networks.
    Fully compatible with StableLearning3's expected attributes.
    """
    def __init__(self, 
                 feature_dim: int, 
                 net_arch: Dict[str, List[int]],
                 activation_fn: Type[torch.nn.Module] = torch.nn.ReLU):
        super().__init__()
        
        # Policy network
        policy_layers = []
        policy_layers.append(torch.nn.Linear(feature_dim, net_arch["pi"][0]))
        policy_layers.append(activation_fn())
        
        for i in range(len(net_arch["pi"]) - 1):
            policy_layers.append(torch.nn.Linear(net_arch["pi"][i], net_arch["pi"][i + 1]))
            policy_layers.append(activation_fn())
        
        self.policy_net = torch.nn.Sequential(*policy_layers)
        
        # Value network
        value_layers = []
        value_layers.append(torch.nn.Linear(feature_dim, net_arch["vf"][0]))
        value_layers.append(activation_fn())
        
        for i in range(len(net_arch["vf"]) - 1):
            value_layers.append(torch.nn.Linear(net_arch["vf"][i], net_arch["vf"][i + 1]))
            value_layers.append(activation_fn())
        
        self.value_net = torch.nn.Sequential(*value_layers)
        
        # Critical: Add these attributes that StableLearning3 expects
        self.latent_dim_pi = net_arch["pi"][-1]
        self.latent_dim_vf = net_arch["vf"][-1]
    
    def forward_actor(self, features):
        return self.policy_net(features)
    
    def forward_critic(self, features):
        return self.value_net(features)
    
    def forward(self, features):
        return self.forward_actor(features), self.forward_critic(features)

class NetworkRecordingCallback(BaseCallback):
    """Callback for recording detailed network information during training"""
    def __init__(self, log_dir, record_freq=1000, verbose=0):
        super().__init__(verbose)
        self.log_dir = log_dir
        self.record_freq = record_freq
        self.writer = None
        self.last_weights = {}
        
    def _get_layer_info(self, name):
        """Convert parameter name to human-readable description"""
        parts = name.split('.')
        
        if 'features_extractor' in name:
            if 'extractors' in name:
                # Extract which observation type this processes
                for idx, part in enumerate(parts):
                    if part == 'extractors' and idx + 1 < len(parts):
                        obs_type = parts[idx + 1]
                        layer_type = 'unknown'
                        if 'weight' in parts[-1]:
                            layer_type = 'weights'
                        elif 'bias' in parts[-1]:
                            layer_type = 'bias'
                        return f"Feature Extractor - {obs_type} observation - {layer_type}"
            
            if 'phase_embedding' in name:
                return "Phase Embedding Layer"
                
            if 'final_projection' in name:
                layer_num = -1
                for idx, part in enumerate(parts):
                    if part.isdigit():
                        layer_num = int(part)
                return f"Final Projection Layer {layer_num}"
                
            if 'lstm' in name:
                return "Length-One Gated Feature Layer"
                
        if 'mlp_extractor' in name:
            if 'policy_net' in name:
                # Extract which policy network layer
                layer_num = -1
                for idx, part in enumerate(parts):
                    if part.isdigit():
                        layer_num = int(part)
                return f"Policy Network Layer {layer_num}"
                
            if 'value_net' in name:
                # Extract which value network layer
                layer_num = -1
                for idx, part in enumerate(parts):
                    if part.isdigit():
                        layer_num = int(part)
                return f"Value Network Layer {layer_num}"
        
        return name  # Fall back to original name if pattern not recognized
            
    def _init_callback(self):
        # Initialize the TensorBoard writer if not already done
        from torch.utils.tensorboard import SummaryWriter
        os.makedirs(self.log_dir, exist_ok=True)
        self.writer = SummaryWriter(self.log_dir)
        
        # Create network architecture visualization
        self._create_network_visualization()
        
        # Skip the graph visualization but record network structure as text
        if hasattr(self.model, "policy") and hasattr(self.model.policy, "features_extractor"):
            try:
                # Create a text description of the network instead
                feature_extractor = self.model.policy.features_extractor
                network_info = {}
                
                # Basic network info
                network_info["type"] = str(type(feature_extractor).__name__)
                network_info["output_dim"] = feature_extractor.output_dim
                
                # Record extractors
                extractors_info = {}
                for key, module in feature_extractor.extractors.items():
                    extractors_info[key] = str(module)
                network_info["extractors"] = extractors_info
                
                # Record other modules
                network_info["phase_embedding"] = str(feature_extractor.phase_embedding)
                network_info["final_projection"] = str(feature_extractor.final_projection)
                network_info["lstm"] = str(feature_extractor.lstm)
                
                # Count parameters
                param_count = sum(p.numel() for p in feature_extractor.parameters() if p.requires_grad)
                network_info["trainable_parameters"] = param_count
                
                # Create and log parameter tables with descriptions
                param_table = "| Layer | Shape | Parameters | Description |\n"
                param_table += "|-------|-------|------------|-------------|\n"
                
                for name, param in self.model.policy.named_parameters():
                    if param.requires_grad:
                        layer_info = self._get_layer_info(name)
                        shape_str = ' × '.join([str(dim) for dim in param.shape])
                        param_count = param.numel()
                        param_table += f"| {name} | {shape_str} | {param_count:,} | {layer_info} |\n"
                        
                        # Log initial parameter histograms with readable names
                        self.writer.add_histogram(f"init_parameters/{layer_info}", param.detach(), 0)
                        # Store initial weights for comparison
                        self.last_weights[name] = param.data.clone()
                
                # Log parameter table
                self.writer.add_text("Network Parameters", param_table)
                # Log network architecture info
                self.writer.add_text("Network Architecture", str(network_info))
                
                logging.info("Recorded network architecture with detailed parameter information")
            except Exception as e:
                logging.warning(f"Could not record network architecture: {e}")
    
    def _create_network_visualization(self):
        """Create a visual representation of the network architecture"""
        try:
            import matplotlib.pyplot as plt
            import matplotlib.patches as patches
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
            import numpy as np
            
            if not hasattr(self.model, "policy"):
                return
                
            fig = Figure(figsize=(12, 8))
            canvas = FigureCanvas(fig)
            ax = fig.add_subplot(111)
            
            # Collect network structure information
            layers = []
            
            # Feature extractor
            if hasattr(self.model.policy, "features_extractor"):
                feature_extractor = self.model.policy.features_extractor
                layers.append(("Input", "Observation Space"))
                
                # Add extractor layers
                for key in feature_extractor.extractors:
                    layers.append(("Feature Extractor", f"{key} observation"))
                
                # Add phase embedding if it exists
                if hasattr(feature_extractor, "phase_embedding"):
                    layers.append(("Feature Extractor", "Phase Embedding"))
                
                # Add final projection
                if hasattr(feature_extractor, "final_projection"):
                    layers.append(("Feature Extractor", "Final Projection"))
                    
                # Add the checkpoint-compatible gated feature layer if present.
                if hasattr(feature_extractor, "lstm"):
                    layers.append(("Feature Extractor", "Length-One Gated Layer"))
                
                layers.append(("Feature Extractor Output", f"Dim: {feature_extractor.output_dim}"))
            
            # Policy and value networks
            if hasattr(self.model.policy, "mlp_extractor"):
                mlp = self.model.policy.mlp_extractor
                
                # Policy network
                if hasattr(mlp, "policy_net"):
                    for i, _ in enumerate(mlp.policy_net):
                        if i % 2 == 0:  # Only count linear layers, not activations
                            layers.append(("Policy Network", f"Layer {i//2 + 1}"))
                
                # Value network
                if hasattr(mlp, "value_net"):
                    for i, _ in enumerate(mlp.value_net):
                        if i % 2 == 0:  # Only count linear layers, not activations
                            layers.append(("Value Network", f"Layer {i//2 + 1}"))
            
            layers.append(("Output", "Actions & Value"))
            
            # Draw network diagram
            y_pos = np.arange(len(layers)) * 0.8
            colors = {
                "Input": "lightblue",
                "Feature Extractor": "lightgreen",
                "Feature Extractor Output": "palegreen",
                "Policy Network": "lightsalmon",
                "Value Network": "plum",
                "Output": "peachpuff"
            }
            
            for i, (layer_type, label) in enumerate(layers):
                color = colors.get(layer_type, "lightgray")
                rect = patches.Rectangle((0.1, y_pos[i]-0.3), 0.8, 0.6, 
                                         linewidth=1, edgecolor='black', facecolor=color, alpha=0.7)
                ax.add_patch(rect)
                ax.text(0.5, y_pos[i], f"{layer_type}: {label}", 
                        horizontalalignment='center', verticalalignment='center')
                
                # Add connecting lines between layers
                if i > 0:
                    ax.plot([0.5, 0.5], [y_pos[i-1]+0.3, y_pos[i]-0.3], 'k-', alpha=0.5)
            
            ax.set_xlim(0, 1)
            ax.set_ylim(-0.5, max(y_pos) + 0.5)
            ax.axis('off')
            ax.set_title('Neural Network Architecture')
            
            # Save figure
            fig.tight_layout()
            canvas.draw()
            
            # Convert to numpy array and save
            s, (width, height) = canvas.print_to_buffer()
            image_array = np.frombuffer(s, np.uint8).reshape((height, width, 4))
            
            # Convert to RGB for TensorBoard (which expects shape [height, width, 3])
            image_array = image_array[:, :, :3]
            
            # Add to TensorBoard
            self.writer.add_image('Network Architecture', image_array, dataformats='HWC')
            
            # Save as file
            fig.savefig(os.path.join(self.log_dir, "network_architecture.png"))
            logging.info(f"Created network architecture visualization")
            
        except Exception as e:
            logging.warning(f"Could not create network visualization: {e}")
        
    def _on_step(self):
        # Record weights and biases periodically
        if self.n_calls % self.record_freq == 0:
            # Log weights
            for name, param in self.model.policy.named_parameters():
                if param.requires_grad:
                    # Get readable layer name
                    layer_info = self._get_layer_info(name)
                    
                    # Log full parameter histograms with readable names
                    self.writer.add_histogram(f"weights/{layer_info}", param.data, self.n_calls)
                    
                    # Track weight changes
                    if name in self.last_weights:
                        weight_change = torch.norm(param.data - self.last_weights[name]).item()
                        self.writer.add_scalar(f"weight_change/{layer_info}", weight_change, self.n_calls)
                        # Update stored weights
                        self.last_weights[name] = param.data.clone()
                    
                    # Track weight statistics with readable names
                    self.writer.add_scalar(f"weight_mean/{layer_info}", param.data.mean().item(), self.n_calls)
                    self.writer.add_scalar(f"weight_std/{layer_info}", param.data.std().item(), self.n_calls)
                    self.writer.add_scalar(f"weight_max/{layer_info}", param.data.abs().max().item(), self.n_calls)
            
            # Track gradients if available
            for name, param in self.model.policy.named_parameters():
                if param.requires_grad and param.grad is not None:
                    # Get readable layer name
                    layer_info = self._get_layer_info(name)
                    
                    # Log gradient histograms with readable names
                    self.writer.add_histogram(f"gradients/{layer_info}", param.grad, self.n_calls)
                    # Compute gradient norm
                    grad_norm = torch.norm(param.grad).item()
                    self.writer.add_scalar(f"grad_norm/{layer_info}", grad_norm, self.n_calls)
            
            # Record learning rate
            if hasattr(self.model, "learning_rate") and hasattr(self.model.learning_rate, "current_lr"):
                self.writer.add_scalar("train/learning_rate", 
                                      self.model.learning_rate.current_lr, 
                                      self.n_calls)
                                      
            # Log top activated neurons (if we're far enough into training)
            if self.n_calls > 10000 and hasattr(self.model, "policy") and hasattr(self.model.policy, "features_extractor"):
                self._log_neuron_activations()
                
        return True
        
    def _log_neuron_activations(self):
        """Log which neurons activate most strongly for different game states"""
        try:
            # This would require gathering activation data during forward passes
            # We can implement a simplified version that logs the weight magnitudes instead
            
            # For each layer in the policy network
            if hasattr(self.model.policy, "mlp_extractor") and hasattr(self.model.policy.mlp_extractor, "policy_net"):
                policy_net = self.model.policy.mlp_extractor.policy_net
                
                # Create a table for the top neurons by weight magnitude
                for i in range(0, len(policy_net), 2):  # Skip activation layers
                    if isinstance(policy_net[i], torch.nn.Linear):
                        layer = policy_net[i]
                        weights = layer.weight.data
                        
                        # Compute L2 norm of each neuron's weights
                        neuron_importance = torch.norm(weights, dim=1)
                        
                        # Get top 5 neurons
                        top_values, top_indices = torch.topk(neuron_importance, min(5, len(neuron_importance)))
                        
                        # Create a table for TensorBoard
                        neuron_table = "| Neuron | Weight Magnitude | Possible Interpretation |\n"
                        neuron_table += "|--------|-----------------|-------------------------|\n"
                        
                        for idx, (neuron_idx, magnitude) in enumerate(zip(top_indices.tolist(), top_values.tolist())):
                            # A simplified interpretation based on index
                            # In reality, this would require more sophisticated analysis
                            neuron_table += f"| Neuron {neuron_idx} | {magnitude:.4f} | Unknown - needs gameplay correlation |\n"
                        
                        # Log the table
                        self.writer.add_text(f"Top Neurons/Policy Layer {i//2}", neuron_table, self.n_calls)
        except Exception as e:
            logging.warning(f"Could not log neuron activations: {e}")
    
    def _on_training_end(self):
        # Save a copy of the feature extractor and policy network
        if hasattr(self.model, "policy"):
            # Save feature extractor state dict
            torch.save(
                self.model.policy.features_extractor.state_dict(),
                os.path.join(self.log_dir, "feature_extractor.pth")
            )
            
            # Save policy network state dict
            if hasattr(self.model.policy, "mlp_extractor"):
                torch.save(
                    self.model.policy.mlp_extractor.state_dict(),
                    os.path.join(self.log_dir, "policy_network.pth")
                )
                
            logging.info(f"Saved network parameters to {self.log_dir}")
            
        # Close the writer
        if self.writer is not None:
            self.writer.close()

class ResourceMonitorCallback(BaseCallback):
    """Monitor and record system resources during training"""
    def __init__(self, log_dir, monitor_freq=1000, verbose=0):
        super().__init__(verbose)
        self.log_dir = log_dir
        self.monitor_freq = monitor_freq
        self.writer = None
        self.psutil_available = False
        self.gputil_available = False
        self._monitor_stop = threading.Event()
        self._monitor_thread = None
        self._sample_index = 0
        
        # Check for optional dependencies
        try:
            import psutil
            self.psutil_available = True
        except ImportError:
            logging.warning("psutil not available. CPU and RAM monitoring disabled.")
            
        try:
            import GPUtil
            self.GPUtil = GPUtil
            self.gputil_available = True
        except ImportError:
            logging.warning("GPUtil not available. GPU monitoring disabled.")

    def _tensorboard_step(self):
        """Use policy transitions as the single resource-metric x-axis."""
        return int(getattr(self, 'num_timesteps', 0))
    
    def _init_callback(self):
        # Initialize TensorBoard writer
        from torch.utils.tensorboard import SummaryWriter
        os.makedirs(self.log_dir, exist_ok=True)
        self.writer = SummaryWriter(self.log_dir)
        
        # Record system specs at start
        if self.psutil_available:
            import psutil
            # Get CPU info
            cpu_count = psutil.cpu_count(logical=False)
            cpu_count_logical = psutil.cpu_count(logical=True)
            self.writer.add_text("system/cpu_info", 
                                f"Physical cores: {cpu_count}, Logical cores: {cpu_count_logical}")
            
            # Get memory info
            mem = psutil.virtual_memory()
            total_ram_gb = mem.total / (1024**3)
            self.writer.add_text("system/memory_info", f"Total RAM: {total_ram_gb:.2f} GB")
            
        # Record GPU info if available
        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                gpu_name = torch.cuda.get_device_name(i)
                self.writer.add_text(f"system/gpu{i}_info", f"Name: {gpu_name}")
        
        logging.info("Resource monitoring initialized")
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="playersim-resource-monitor",
            daemon=True)
        self._monitor_thread.start()

    def _monitor_loop(self):
        """Sample independently so PPO learning bursts are not missed."""
        process = None
        psutil_module = None
        child_processes = {}
        if self.psutil_available:
            import psutil as psutil_module
            process = psutil_module.Process()
            process.cpu_percent(None)
        while not self._monitor_stop.wait(1.0):
            try:
                self._sample_index += 1
                step = self._tensorboard_step()
                self.writer.add_scalar(
                    "system/resource_sample_index", self._sample_index, step)
                if process is not None:
                    children = process.children(recursive=True)
                    process_cpu = process.cpu_percent(None)
                    child_cpu = 0.0
                    child_rss = 0
                    for child in children:
                        try:
                            # psutil stores the previous CPU sample on the Process
                            # object. Keep one object per PID instead of recreating
                            # it every second, which always reports an initial 0%.
                            tracked_child = child_processes.get(child.pid)
                            if tracked_child is None:
                                tracked_child = child
                                child_processes[child.pid] = tracked_child
                                tracked_child.cpu_percent(None)
                            else:
                                child_cpu += tracked_child.cpu_percent(None)
                            child_rss += child.memory_info().rss
                        except (psutil_module.NoSuchProcess,
                                psutil_module.AccessDenied):
                            continue
                    live_pids = {child.pid for child in children}
                    child_processes = {
                        pid: child for pid, child in child_processes.items()
                        if pid in live_pids
                    }
                    self.writer.add_scalar(
                        "system/process_cpu_percent", process_cpu, step)
                    self.writer.add_scalar(
                        "system/worker_cpu_percent", child_cpu, step)
                    self.writer.add_scalar(
                        "system/process_tree_ram_gb",
                        (process.memory_info().rss + child_rss) / (1024 ** 3),
                        step)
                if self.gputil_available:
                    for gpu in self.GPUtil.getGPUs():
                        self.writer.add_scalar(
                            f"system/gpu{gpu.id}_utilization_percent",
                            float(gpu.load) * 100.0, step)
                        self.writer.add_scalar(
                            f"system/gpu{gpu.id}_memory_utilization_percent",
                            float(gpu.memoryUtil) * 100.0, step)
                        self.writer.add_scalar(
                            f"system/gpu{gpu.id}_temperature_c",
                            float(gpu.temperature), step)
                if torch.cuda.is_available():
                    for index in range(torch.cuda.device_count()):
                        self.writer.add_scalar(
                            f"system/cuda{index}_allocated_gb",
                            torch.cuda.memory_allocated(index) / (1024 ** 3),
                            step)
                        self.writer.add_scalar(
                            f"system/cuda{index}_reserved_gb",
                            torch.cuda.memory_reserved(index) / (1024 ** 3),
                            step)
            except Exception as monitor_error:
                logging.debug(
                    "Background resource sample failed: %s", monitor_error)
    
    def _on_step(self):
        try:
            return self._monitor_step()
        except Exception as monitor_error:
            # System telemetry must never take a training run down (the
            # background sampler already swallows its own failures the same
            # way): a deleted/rotated event file only costs metrics.
            logging.warning(
                "Resource monitor step failed: %s", monitor_error)
            return True

    def _monitor_step(self):
        if self.n_calls % self.monitor_freq == 0:
            step = self._tensorboard_step()
            # Monitor CPU and RAM
            if self.psutil_available:
                import psutil
                # CPU usage per core
                cpu_percent_per_core = psutil.cpu_percent(percpu=True)
                for i, percent in enumerate(cpu_percent_per_core):
                    self.writer.add_scalar(f"system/cpu_core{i}_percent", percent, step)
                
                # Overall CPU usage
                cpu_percent = psutil.cpu_percent()
                self.writer.add_scalar("system/cpu_percent", cpu_percent, step)
                
                # RAM usage (GB)
                ram = psutil.virtual_memory()
                ram_used_gb = ram.used / (1024**3)
                ram_percent = ram.percent
                
                self.writer.add_scalar("system/ram_used_gb", ram_used_gb, step)
                self.writer.add_scalar("system/ram_percent", ram_percent, step)
                
                # Disk usage
                disk = psutil.disk_usage('/')
                disk_percent = disk.percent
                self.writer.add_scalar("system/disk_percent", disk_percent, step)
                
                # Network IO
                net_io = psutil.net_io_counters()
                self.writer.add_scalar("system/net_sent_mb", net_io.bytes_sent / (1024**2), step)
                self.writer.add_scalar("system/net_recv_mb", net_io.bytes_recv / (1024**2), step)
                
                if self.verbose > 0:
                    logging.info(f"Step {self.n_calls}: CPU: {cpu_percent}% RAM: {ram_used_gb:.1f} GB ({ram_percent}%)")
            
            # The one-second background sampler is the sole owner of CUDA
            # TensorBoard tags. Keeping a second writer here interleaved
            # VecEnv-call steps with wall-clock sample steps in one series.
            if torch.cuda.is_available():
                for i in range(torch.cuda.device_count()):
                    mem_allocated = torch.cuda.memory_allocated(i) / (1024**3)  # GB
                    mem_reserved = torch.cuda.memory_reserved(i) / (1024**3)  # GB
                    if self.verbose > 0:
                        logging.info(f"CUDA {i}: Allocated: {mem_allocated:.2f} GB, Reserved: {mem_reserved:.2f} GB")
        
        return True
    
    def _on_training_end(self):
        self._monitor_stop.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=2.0)
        if self.writer is not None:
            self.writer.close()
            
# Keep the host's stream objects intact. Replacing them during import used to
# reorder buffered output and failed in IDE/notebook hosts whose streams do not
# expose ``.buffer``.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('deck_stats.log', encoding='utf-8', errors='replace'),
        logging.StreamHandler(sys.stdout)
    ]
)


def configure_runtime_logging(*, debug=False, worker=False):
    """Keep training workers quiet and make all console streams UTF-8-safe."""
    from Playersim import debug as debug_module
    from Playersim import environment as environment_module

    debug_module.DEBUG_MODE = bool(debug)
    debug_module.DEBUG_ENV_RESETS = bool(debug)
    debug_module.DEBUG_ACTION_STEPS = bool(debug)
    environment_module.DEBUG_MODE = bool(debug)
    environment_module.DEBUG_ACTION_STEPS = bool(debug)

    # A non-debug worker emits only WARNING+, so its catch-all debug handler
    # otherwise produces a byte-for-byte duplicate of the warning file (and a
    # second copy of every error).  Keep the dedicated warning/error files and
    # create a worker debug file only when debug logging was requested.
    debug_module.debug_handler.setLevel(
        logging.DEBUG if (debug or not worker) else logging.CRITICAL + 1)

    level = logging.DEBUG if debug else (logging.WARNING if worker else logging.INFO)
    console_level = logging.DEBUG if debug else (
        logging.ERROR if worker else logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in root_logger.handlers:
        if (isinstance(handler, logging.StreamHandler)
                and not isinstance(handler, logging.FileHandler)):
            handler.setLevel(console_level)
            stream = getattr(handler, "stream", None)
            if hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except (OSError, ValueError):
                    pass

# Optimization and Configuration
def safe_cpu_count():
    """Return a usable logical CPU count even on constrained/unknown hosts."""
    return max(1, os.cpu_count() or 1)


torch.set_num_threads(safe_cpu_count())
torch.set_float32_matmul_precision('high')

# Path Configuration
VERSION = "ALPHA_ZERO_MTG_V3.00"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FORMAT_NAME = "standard"
DEFAULT_FORMAT_DIR = os.path.join(BASE_DIR, "formats", DEFAULT_FORMAT_NAME)
DECKS_DIR = os.path.join(DEFAULT_FORMAT_DIR, "decks")
MODEL_DIR = os.path.join(BASE_DIR, "models")
LOG_DIR = os.path.join(BASE_DIR, "logs")
TENSORBOARD_DIR = os.path.join(BASE_DIR, "tensorboard_logs")

TRAINING_MANIFEST_SCHEMA_VERSION = 1
EVALUATION_SEED_OFFSET = 1_000_000


def utc_timestamp():
    """Return a stable, timezone-aware timestamp for run artifacts."""
    return datetime.now(timezone.utc).isoformat()


def json_safe(value):
    """Convert training configuration values into deterministic JSON data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    if isinstance(value, dict):
        return {
            str(key): json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return str(value)


def write_json_atomic(path, payload):
    """Atomically publish a UTF-8 JSON artifact in its destination directory."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(json_safe(payload), handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_path, path)


def write_bytes_atomic(path, payload):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_path, path)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_artifact_path(path):
    """Resolve an artifact path, accepting SB3's automatically-added .zip."""
    candidates = [path]
    if not str(path).lower().endswith(".zip"):
        candidates.append(f"{path}.zip")
    return next((candidate for candidate in candidates
                 if os.path.isfile(candidate)), None)


def artifact_identity(path):
    """Return a portable identity for an artifact, accepting SB3's .zip suffix."""
    actual_path = resolve_artifact_path(path)
    if actual_path is None:
        return None
    try:
        display_path = os.path.relpath(actual_path, BASE_DIR)
    except ValueError:
        display_path = os.path.abspath(actual_path)
    return {
        "path": display_path.replace(os.sep, "/"),
        "size_bytes": os.path.getsize(actual_path),
        "sha256": sha256_file(actual_path),
    }


def git_provenance():
    """Capture the exact source revision without making Git a hard dependency."""
    def run_git(*arguments):
        try:
            completed = subprocess.run(
                ["git", "-C", BASE_DIR, *arguments],
                capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()

    revision = run_git("rev-parse", "HEAD")
    branch = run_git("rev-parse", "--abbrev-ref", "HEAD")
    status = run_git("status", "--porcelain", "--untracked-files=normal")
    dirty_paths = []
    if status:
        for line in status.splitlines():
            fields = line.strip().split(maxsplit=1)
            if len(fields) == 2:
                dirty_paths.append(fields[1])
        dirty_paths.sort()
    return {
        "revision": revision,
        "branch": branch,
        "dirty": None if status is None else bool(status),
        "dirty_paths": dirty_paths,
    }


def capture_working_tree_patch(run_model_dir):
    """Persist the tracked source delta so a dirty training run is reproducible."""
    try:
        completed = subprocess.run(
            ["git", "-C", BASE_DIR, "diff", "--binary", "HEAD"],
            capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    patch_path = os.path.join(run_model_dir, "source_worktree.patch")
    write_bytes_atomic(patch_path, completed.stdout)
    return artifact_identity(patch_path)


def dependency_versions():
    distributions = {
        "gymnasium": "gymnasium",
        "numpy": "numpy",
        "optuna": "optuna",
        "psutil": "psutil",
        "sb3_contrib": "sb3-contrib",
        "stable_baselines3": "stable-baselines3",
        "tensorboard": "tensorboard",
        "torch": "torch",
    }
    versions = {}
    for key, distribution in distributions.items():
        try:
            versions[key] = importlib_metadata.version(distribution)
        except importlib_metadata.PackageNotFoundError:
            versions[key] = None
    return versions


def installed_distribution_versions():
    """Capture the complete Python environment, including transitive packages."""
    versions = {}
    for distribution in importlib_metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            versions[name] = distribution.version
    return dict(sorted(versions.items(), key=lambda pair: pair[0].casefold()))


def runtime_provenance(*, cpu_only):
    cuda_available = bool(torch.cuda.is_available() and not cpu_only)
    devices = []
    if cuda_available:
        for index in range(torch.cuda.device_count()):
            try:
                capability = list(torch.cuda.get_device_capability(index))
            except Exception:
                capability = None
            devices.append({
                "index": index,
                "name": torch.cuda.get_device_name(index),
                "compute_capability": capability,
            })
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "logical_cpus": safe_cpu_count(),
        "selected_device": "cuda" if cuda_available else "cpu",
        "cpu_only_requested": bool(cpu_only),
        "torch_cuda_version": torch.version.cuda,
        "cudnn_version": torch.backends.cudnn.version(),
        "cuda_devices": devices,
        "deterministic_algorithms": torch.are_deterministic_algorithms_enabled(),
        "dependencies": dependency_versions(),
        "installed_distributions": installed_distribution_versions(),
    }


def deck_provenance(decks, card_db, decks_dir=DECKS_DIR):
    """Identify both the loaded decks and every JSON source used to build them."""
    source_files = []
    if os.path.isdir(decks_dir):
        paths = []
        for root, _, filenames in os.walk(decks_dir):
            for filename in filenames:
                if filename.lower().endswith(".json"):
                    paths.append(os.path.join(root, filename))
        for path in sorted(
                paths,
                key=lambda item: os.path.relpath(
                    item, decks_dir).replace(os.sep, "/").casefold()):
            source_files.append({
                "path": os.path.relpath(path, BASE_DIR).replace(os.sep, "/"),
                "size_bytes": os.path.getsize(path),
                "sha256": sha256_file(path),
            })
    loaded_decks = []
    for index, deck in enumerate(decks):
        if isinstance(deck, dict):
            loaded_decks.append({
                "name": deck.get("name"),
                "card_count": len(deck.get("cards", [])),
            })
        else:
            loaded_decks.append({
                "name": f"non-dict-deck-{index}",
                "card_count": None,
            })
    return {
        "deck_count": len(decks),
        "unique_card_count": len(card_db),
        "loaded_decks": loaded_decks,
        "source_files": source_files,
    }


def load_training_corpus(decks_arg, format_name, format_dir_arg):
    """Resolve corpus/format flags into (decks, card_db, decks_dir, lineage).

    With no flags this loads the pinned hydrated Standard metagame corpus.
    All training corpora load strictly (no backup-deck fallback), and a format
    request applies the frozen ``formats/<format>`` canonical card registry
    and feature schema.
    """
    from Playersim.card_registry import format_lineage, load_format_namespace

    format_name = format_name or DEFAULT_FORMAT_NAME
    format_dir = format_dir_arg
    if format_dir is None and format_name:
        format_dir = os.path.join(BASE_DIR, "formats", format_name)
    decks_dir = (
        os.path.abspath(decks_arg) if decks_arg
        else os.path.join(BASE_DIR, "formats", format_name, "decks"))
    card_registry = feature_schema = None
    if format_dir is not None:
        card_registry, feature_schema = load_format_namespace(format_dir)
        logging.info(
            "Using frozen format namespace %s (registry %s cards, "
            "feature_dim %s)", format_dir, len(card_registry["cards"]),
            feature_schema["feature_dim"])
    decks, card_db = load_decks_and_card_db(
        decks_dir, format_name=format_name,
        strict_legality=True,
        card_registry=card_registry, feature_schema=feature_schema)
    lineage = format_lineage(
        decks_dir, format_name=format_name,
        card_registry=card_registry, feature_schema=feature_schema)
    return decks, card_db, decks_dir, lineage


def training_artifacts(run_model_dir, run_id):
    checkpoint_dir = os.path.join(run_model_dir, "checkpoints")
    checkpoints = []
    if os.path.isdir(checkpoint_dir):
        for filename in sorted(os.listdir(checkpoint_dir), key=str.casefold):
            identity = artifact_identity(os.path.join(checkpoint_dir, filename))
            if identity is not None:
                checkpoints.append(identity)
    return {
        "final_model": artifact_identity(os.path.join(run_model_dir, "final_model")),
        "failed_model": artifact_identity(os.path.join(run_model_dir, "failed_model")),
        "best_model": artifact_identity(
            os.path.join(run_model_dir, "best_model", "best_model")),
        "evaluation_history": artifact_identity(os.path.join(
            LOG_DIR, run_id, "evaluation", "evaluations.npz")),
        "feature_extractor": artifact_identity(
            os.path.join(run_model_dir, "feature_extractor.pth")),
        "network_summary": artifact_identity(os.path.join(
            run_model_dir, "architecture", "network_summary.txt")),
        "checkpoints": checkpoints,
    }


def evaluation_history_summary(run_id):
    """Summarize EvalCallback output without making the NPZ the only record."""
    path = os.path.join(LOG_DIR, run_id, "evaluation", "evaluations.npz")
    if not os.path.isfile(path):
        return {"status": "not_run", "evaluation_points": 0}
    try:
        with np.load(path, allow_pickle=False) as data:
            timesteps = np.asarray(data["timesteps"], dtype=np.int64)
            results = np.asarray(data["results"], dtype=np.float64)
            episode_lengths = np.asarray(
                data["ep_lengths"], dtype=np.float64)
    except (OSError, KeyError, ValueError) as error:
        return {"status": "unreadable", "error": str(error)}
    return {
        "status": "passed",
        "evaluation_points": int(len(timesteps)),
        "timesteps": timesteps.tolist(),
        "episodes_per_evaluation": (
            int(results.shape[1]) if results.ndim >= 2 else None),
        "mean_rewards": np.mean(results, axis=1).tolist(),
        "mean_episode_lengths": np.mean(episode_lengths, axis=1).tolist(),
    }


def training_fidelity_failure(info):
    if not isinstance(info, dict):
        return None
    game_result = str(info.get("game_result", ""))
    severe_flags = {
        key: info.get(key)
        for key in (
            "critical_error", "execution_failed",
            "opponent_execution_failed", "invalid_action", "error_reset",
            "episode_step_limit")
        if info.get(key)
    }
    if game_result.startswith("error") or game_result in {
            "invalid_limit", "aborted"}:
        severe_flags["game_result"] = game_result
    if not severe_flags:
        return None
    return (info.get("error_message") or info.get("invalid_action_reason")
            or severe_flags)


def rollout_signature(observation, masks, actions, env_index):
    """Hash one vectorized policy decision without serializing large arrays."""
    signature_digest = hashlib.sha256()
    if isinstance(observation, dict):
        for key, value in sorted(observation.items()):
            signature_digest.update(key.encode("utf-8"))
            signature_digest.update(
                np.ascontiguousarray(np.asarray(value)[env_index]).tobytes())
    else:
        signature_digest.update(
            np.ascontiguousarray(np.asarray(observation)[env_index]).tobytes())
    if masks is not None:
        signature_digest.update(
            np.ascontiguousarray(np.asarray(masks)[env_index]).tobytes())
    signature_digest.update(
        np.ascontiguousarray(np.asarray(actions)[env_index]).tobytes())
    return signature_digest.digest()


def repeated_short_cycle_period(signatures, *, max_period=4, repeats=3):
    """Return a repeated suffix's period, or None when progress is monotonic."""
    for period in range(1, max_period + 1):
        required = period * repeats
        if len(signatures) < required:
            continue
        suffix = signatures[-required:]
        pattern = suffix[:period]
        if all(suffix[offset:offset + period] == pattern
               for offset in range(period, required, period)):
            return period
    return None


class StrictEvaluationVecEnv(VecEnvWrapper):
    """Make periodic evaluation fail fast on fidelity errors or short cycles."""

    def __init__(self, venv, *, max_cycle_period=4, cycle_repeats=3):
        super().__init__(venv)
        self.max_cycle_period = max_cycle_period
        self.cycle_repeats = cycle_repeats
        self._last_observation = None
        self._signature_histories = [[] for _ in range(self.num_envs)]

    def reset(self):
        self._signature_histories = [[] for _ in range(self.num_envs)]
        self._last_observation = self.venv.reset()
        return self._last_observation

    def step_async(self, actions):
        actions = np.asarray(actions, dtype=np.int64).reshape(-1)
        if self._last_observation is not None:
            masks = np.asarray(get_action_masks(self.venv), dtype=bool)
            for env_index in range(self.num_envs):
                history = self._signature_histories[env_index]
                history.append(rollout_signature(
                    self._last_observation, masks, actions, env_index))
                keep = self.max_cycle_period * self.cycle_repeats
                if len(history) > keep:
                    del history[:-keep]
                period = repeated_short_cycle_period(
                    history,
                    max_period=self.max_cycle_period,
                    repeats=self.cycle_repeats)
                if period is not None:
                    raise RuntimeError(
                        "Strict evaluation detected a non-progressing policy "
                        f"cycle of period {period} in environment {env_index}")
        self.venv.step_async(actions)

    def step_wait(self):
        observations, rewards, dones, infos = self.venv.step_wait()
        for env_index, info in enumerate(infos):
            failure = training_fidelity_failure(info)
            if failure is not None:
                raise RuntimeError(
                    "Strict evaluation fidelity failure in environment "
                    f"{env_index}: {failure}")
            if dones[env_index]:
                self._signature_histories[env_index].clear()
        self._last_observation = observations
        return observations, rewards, dones, infos


def validate_training_checkpoint(path, env, *, device, seed):
    """Reload a checkpoint and prove deterministic rollout progress is clean."""
    checkpoint_path = resolve_artifact_path(path)
    if checkpoint_path is None:
        raise FileNotFoundError(f"Saved checkpoint was not published at {path}")
    loaded = MaskablePPO.load(checkpoint_path, env=env, device=device)
    if hasattr(loaded, "set_random_seed"):
        loaded.set_random_seed(seed)
    if hasattr(env, "seed"):
        env.seed(seed)
    observation = env.reset()
    signature_histories = [[] for _ in range(env.num_envs)]
    episodes_completed = 0
    validation_steps = 256
    for _ in range(validation_steps):
        masks = np.asarray(get_action_masks(env), dtype=bool)
        if masks.ndim != 2 or not masks.any(axis=1).all():
            raise RuntimeError(
                "Reloaded checkpoint environment produced an invalid mask")
        actions, _ = loaded.predict(
            observation, deterministic=True, action_masks=masks)
        actions = np.asarray(actions, dtype=np.int64).reshape(-1)
        if len(actions) != masks.shape[0] or not all(
                masks[index, action] for index, action in enumerate(actions)):
            raise RuntimeError(
                "Reloaded checkpoint selected a mask-invalid action")

        for env_index in range(env.num_envs):
            history = signature_histories[env_index]
            history.append(rollout_signature(
                observation, masks, actions, env_index))
            if len(history) > 12:
                del history[:-12]
            period = repeated_short_cycle_period(history)
            if period is not None:
                raise RuntimeError(
                    "Reloaded checkpoint made no public progress while cycling "
                    f"with period {period} in environment {env_index}; "
                    f"latest actions were {actions.tolist()}")

        observation, rewards, dones, infos = env.step(actions)
        if not np.isfinite(np.asarray(rewards, dtype=np.float64)).all():
            raise RuntimeError(
                "Reloaded checkpoint produced a non-finite reward")
        for env_index, info in enumerate(infos):
            failure = training_fidelity_failure(info)
            if failure is not None:
                raise RuntimeError(
                    "Reloaded checkpoint hit a strict fidelity failure in "
                    f"environment {env_index}: {failure}")
            if dones[env_index]:
                signature_histories[env_index].clear()
        episodes_completed += int(np.asarray(dones, dtype=bool).sum())
    return {
        "status": "passed",
        "checkpoint_reload": True,
        "mask_valid_prediction": True,
        "finite_step_reward": True,
        "public_progress": True,
        "short_cycle_periods_checked": 4,
        "rollout_steps": validation_steps,
        "episodes_completed": episodes_completed,
        "validated_seed": int(seed),
    }

# Feature Dimension Configuration
FEATURE_OUTPUT_DIM = 512

NETWORK_ARCHITECTURES = {
    'small': {'pi': [128, 64, 32], 'vf': [128, 64, 32]},
    'medium': {'pi': [256, 128, 64], 'vf': [256, 128, 64]},
    'large': {'pi': [512, 256, 128], 'vf': [512, 256, 128]},
}

ACTIVATION_FUNCTIONS = {
    'relu': torch.nn.ReLU,
    'leaky_relu': torch.nn.LeakyReLU,
    'tanh': torch.nn.Tanh,
}


def build_training_config(args, optuna_params=None):
    """Build the complete MaskablePPO configuration for the final model."""
    config = {
        'learning_rate': args.learning_rate,
        'n_steps': args.n_steps,
        'batch_size': args.batch_size,
        'gamma': 0.995,
        'gae_lambda': 0.95,
        'clip_range': 0.2,
        'clip_range_vf': 0.2,
        'ent_coef': 0.01,
        'vf_coef': 0.5,
        'target_kl': 0.02,
        'net_arch': NETWORK_ARCHITECTURES['medium'],
        'n_epochs': 3,
        'max_grad_norm': 0.5,
        'activation_fn': ACTIVATION_FUNCTIONS['relu'],
        'action_reward_scale':
            AlphaZeroMTGEnv.DEFAULT_ACTION_REWARD_SCALE,
        'state_potential_scale':
            AlphaZeroMTGEnv.DEFAULT_STATE_POTENTIAL_SCALE,
        'reward_contract_version':
            AlphaZeroMTGEnv.REWARD_CONTRACT_VERSION,
    }
    if not optuna_params:
        return config

    expected = {
        'learning_rate', 'n_steps', 'batch_size', 'gamma_complement',
        'gae_lambda', 'clip_range', 'ent_coef', 'policy_neurons',
        'n_epochs', 'max_grad_norm', 'activation_fn',
    }
    unknown = set(optuna_params) - expected
    if unknown:
        raise ValueError(f"Unknown optimized hyperparameters: {sorted(unknown)}")

    for key in (
        'learning_rate', 'n_steps', 'batch_size', 'gae_lambda',
        'clip_range', 'ent_coef', 'n_epochs', 'max_grad_norm',
    ):
        if key in optuna_params:
            config[key] = optuna_params[key]
    if 'gamma_complement' in optuna_params:
        config['gamma'] = 1.0 - optuna_params['gamma_complement']
    if 'policy_neurons' in optuna_params:
        config['net_arch'] = NETWORK_ARCHITECTURES[
            optuna_params['policy_neurons']]
    if 'activation_fn' in optuna_params:
        config['activation_fn'] = ACTIVATION_FUNCTIONS[
            optuna_params['activation_fn']]
    return config


def make_masked_mtg_env(decks, card_db, storage_root, *, agent_is_p1=True,
                        alternate_agent_seat=False, subtype_vocab=None,
                        reward_discount=AlphaZeroMTGEnv.DEFAULT_REWARD_DISCOUNT,
                        action_reward_scale=
                            AlphaZeroMTGEnv.DEFAULT_ACTION_REWARD_SCALE,
                        state_potential_scale=
                            AlphaZeroMTGEnv.DEFAULT_STATE_POTENTIAL_SCALE):
    """Create an environment whose generated statistics stay in one scope."""
    os.makedirs(storage_root, exist_ok=True)
    return ActionMasker(
        AlphaZeroMTGEnv(
            decks,
            card_db,
            deck_stats_path=os.path.join(storage_root, 'deck_stats'),
            card_memory_path=os.path.join(storage_root, 'card_memory'),
            agent_is_p1=agent_is_p1,
            alternate_agent_seat=alternate_agent_seat,
            subtype_vocab=subtype_vocab,
            reward_discount=reward_discount,
            action_reward_scale=action_reward_scale,
            state_potential_scale=state_potential_scale,
        ),
        action_mask_fn='action_mask',
    )

class CustomLearningRateScheduler:
    """Advanced learning rate scheduler with adaptive decay"""
    def __init__(self, initial_lr=3e-4, min_lr=1e-5, decay_factor=0.95):
        self.initial_lr = initial_lr
        self.min_lr = min_lr
        self.decay_factor = decay_factor
        self.current_lr = initial_lr

    def __call__(self, progress_remaining):
        """
        Compute learning rate based on training progress
        
        Args:
            progress_remaining (float): Fraction of training remaining
        
        Returns:
            float: Adjusted learning rate
        """
        # Exponential decay
        self.current_lr = max(
            self.min_lr, 
            self.initial_lr * (self.decay_factor ** (1 - progress_remaining))
        )
        return self.current_lr


def create_training_model(env, training_config, seed=None, device="auto",
                          tensorboard_log=None):
    """Construct the final MaskablePPO model from one complete config."""
    policy_kwargs = {
        'features_extractor_class': FixedWindowMTGExtractor,
        'features_extractor_kwargs': {
            'features_dim': FEATURE_OUTPUT_DIM,
        },
        'net_arch': training_config['net_arch'],
        'activation_fn': training_config['activation_fn'],
    }
    lr_scheduler = CustomLearningRateScheduler(
        initial_lr=training_config['learning_rate'])
    return MaskablePPO(
        policy=FixedDimensionMaskableActorCriticPolicy,
        env=env,
        learning_rate=lr_scheduler,
        tensorboard_log=(tensorboard_log if tensorboard_log is not None
                         else TENSORBOARD_DIR),
        policy_kwargs=policy_kwargs,
        n_steps=training_config['n_steps'],
        batch_size=training_config['batch_size'],
        gamma=training_config['gamma'],
        gae_lambda=training_config['gae_lambda'],
        clip_range=training_config['clip_range'],
        clip_range_vf=training_config['clip_range_vf'],
        ent_coef=training_config['ent_coef'],
        vf_coef=training_config['vf_coef'],
        target_kl=training_config['target_kl'],
        max_grad_norm=training_config['max_grad_norm'],
        verbose=1,
        n_epochs=training_config['n_epochs'],
        seed=seed,
        device=device,
    )

def objective(trial, base_seed=42):
    """
    Advanced Optuna objective function with more sophisticated parameter space
    """
    trial_seed = int(base_seed) + int(getattr(trial, "number", 0))
    set_random_seed(trial_seed)

    # Core hyperparameters
    learning_rate = trial.suggest_float('learning_rate', 1e-5, 3e-4, log=True)
    n_steps = trial.suggest_categorical('n_steps', [1024, 2048, 4096])
    batch_size = trial.suggest_categorical('batch_size', [128, 256, 512])
    
    # Discount factors
    gamma = 1.0 - trial.suggest_float('gamma_complement', 0.0001, 0.1, log=True)
    gae_lambda = trial.suggest_float('gae_lambda', 0.9, 0.999)
    
    # PPO-specific
    clip_range = trial.suggest_float('clip_range', 0.1, 0.3)
    ent_coef = trial.suggest_float('ent_coef', 1e-5, 0.01, log=True)
    
    # Network architecture
    policy_neurons = trial.suggest_categorical('policy_neurons', ['small', 'medium', 'large'])
    net_arch = NETWORK_ARCHITECTURES[policy_neurons]
    
    # Optimization parameters
    n_epochs = trial.suggest_int('n_epochs', 2, 5)
    max_grad_norm = trial.suggest_float('max_grad_norm', 0.3, 0.9)
    
    # Activation function
    activation_name = trial.suggest_categorical('activation_fn', ['relu', 'leaky_relu', 'tanh'])
    activation_fn = ACTIVATION_FUNCTIONS[activation_name]

    # Load decks and card database
    try:
        decks, card_db, _, _ = load_training_corpus(
            None, DEFAULT_FORMAT_NAME, None)
    except Exception as e:
        logging.error(f"Failed to load decks for optimization: {e}")
        return float('-inf')
    format_subtype_vocab = tuple(Card.SUBTYPE_VOCAB)

    # Create environments (fewer environments for hyperparameter optimization).
    # Training and evaluation statistics must not feed the same trackers.
    trial_root = os.path.join(
        LOG_DIR, 'optuna', f"trial_{getattr(trial, 'number', 'unknown')}")
    train_env_index = 0
    eval_env_index = 0

    def make_train_env():
        nonlocal train_env_index
        env_index = train_env_index
        storage = os.path.join(trial_root, 'train', f'env_{env_index}')
        train_env_index += 1
        return make_masked_mtg_env(
            decks, card_db, storage,
            agent_is_p1=(env_index % 2 == 0),
            alternate_agent_seat=True,
            subtype_vocab=format_subtype_vocab,
            reward_discount=gamma)

    def make_eval_env():
        nonlocal eval_env_index
        env_index = eval_env_index
        storage = os.path.join(trial_root, 'eval', f'env_{env_index}')
        eval_env_index += 1
        return make_masked_mtg_env(
            decks, card_db, storage,
            agent_is_p1=(env_index % 2 == 0),
            alternate_agent_seat=True,
            subtype_vocab=format_subtype_vocab,
            reward_discount=gamma)

    # Evaluation must not step the training VecEnv. Doing so leaves PPO's
    # cached ``_last_obs`` out of sync with the environment before the next
    # learn() call and couples evaluation trajectories to training state.
    train_env = make_vec_env(make_train_env, n_envs=2)
    eval_env = make_vec_env(make_eval_env, n_envs=2)
    if hasattr(train_env, "seed"):
        train_env.seed(trial_seed)
    if hasattr(eval_env, "seed"):
        eval_env.seed(trial_seed + EVALUATION_SEED_OFFSET)

    # Construct policy configuration
    policy_kwargs = {
        "features_extractor_class": FixedWindowMTGExtractor,
        "features_extractor_kwargs": {
            "features_dim": FEATURE_OUTPUT_DIM
        },
        "net_arch": net_arch,
        "activation_fn": activation_fn
    }

    try:
        # Create model after both environments are owned by the try/finally so
        # a constructor failure cannot leak worker environments.
        model = MaskablePPO(
            policy=FixedDimensionMaskableActorCriticPolicy,
            env=train_env,
            learning_rate=learning_rate,
            n_steps=n_steps,
            batch_size=batch_size,
            gamma=gamma,
            gae_lambda=gae_lambda,
            clip_range=clip_range,
            clip_range_vf=0.2,
            ent_coef=ent_coef,
            vf_coef=0.5,
            target_kl=0.02,
            policy_kwargs=policy_kwargs,
            verbose=0,
            tensorboard_log=TENSORBOARD_DIR,
            n_epochs=n_epochs,
            max_grad_norm=max_grad_norm,
            seed=trial_seed,
        )

        # Training with pruning support
        for step in range(5):  # 5 evaluation points
            # Train for a short period
            step_size = 20000  # 20k steps per evaluation
            model.learn(total_timesteps=step_size, reset_num_timesteps=(step==0))
            
            # Evaluate current performance
            mean_reward, std_reward = evaluate_policy(
                model, eval_env, n_eval_episodes=5)
            
            # Report to Optuna for pruning decision
            trial.report(mean_reward, step)
            
            # Check if trial should be pruned
            if trial.should_prune():
                raise optuna.TrialPruned()
        
        # Final evaluation with more episodes
        mean_reward, _ = evaluate_policy(
            model, eval_env, n_eval_episodes=10)
        
        return mean_reward
    except optuna.TrialPruned:
        logging.info(f"Trial pruned due to poor performance")
        raise
    except Exception as e:
        import traceback
        tb_str = traceback.format_exc() # Get the full traceback string
        logging.error(f"Hyperparameter trial failed.")
        logging.error(f"--- Exception Type: {type(e).__name__} ---")
        logging.error(f"--- Exception Args: {e.args} ---")
        logging.error(f"--- Full Traceback: ---")
        logging.error(tb_str) # Log the detailed traceback
        logging.error(f"-------------------------")
        # Still return -inf for Optuna or raise TrialPruned
        # raise optuna.TrialPruned() # If you want Optuna to handle it as pruned
        return float('-inf') # If you want Optuna to just record a bad score
    finally:
        train_env.close()
        eval_env.close()

def optimize_hyperparameters(n_trials=50, study_name="mtg_optimization",
                             seed=42):
    """Run Optuna hyperparameter optimization with persistence and pruning"""
    storage_name = f"sqlite:///{study_name}.db"
    study = optuna.create_study(
        study_name=study_name,
        storage=storage_name,
        load_if_exists=True,
        direction='maximize',
        sampler=optuna.samplers.TPESampler(seed=seed),
        pruner=optuna.pruners.MedianPruner()  # Early stopping for bad trials
    )
    
    study.optimize(
        lambda trial: objective(trial, base_seed=seed), n_trials=n_trials)
    
    # Visualization of optimization results
    try:
        import matplotlib.pyplot as plt
        
        # Create optimization plots directory
        plots_dir = os.path.join(BASE_DIR, "optimization_plots")
        os.makedirs(plots_dir, exist_ok=True)
        
        # Plot optimization history
        plt.figure(figsize=(10, 6))
        optuna.visualization.matplotlib.plot_optimization_history(study)
        plt.savefig(os.path.join(plots_dir, f"{study_name}_history.png"))
        
        # Plot parameter importances
        plt.figure(figsize=(10, 6))
        optuna.visualization.matplotlib.plot_param_importances(study)
        plt.savefig(os.path.join(plots_dir, f"{study_name}_importances.png"))
        
        logging.info(f"Optimization plots saved to {plots_dir}")
    except Exception as e:
        logging.warning(f"Could not create optimization plots: {e}")

    return study.best_params

def compare_models(model_path1, model_path2):
    """Compare two saved models to analyze differences in their weights"""
    try:
        # Load the models
        model1 = MaskablePPO.load(model_path1)
        model2 = MaskablePPO.load(model_path2)
        
        print(f"Comparing models:\n  - {model_path1}\n  - {model_path2}")
        
        # Compare feature extractors
        if (hasattr(model1, "policy") and hasattr(model1.policy, "features_extractor") and
            hasattr(model2, "policy") and hasattr(model2.policy, "features_extractor")):
            
            fe1 = model1.policy.features_extractor
            fe2 = model2.policy.features_extractor
            
            print("\n=== FEATURE EXTRACTOR COMPARISON ===")
            
            # Compare weights for each parameter
            for (name1, param1), (name2, param2) in zip(
                fe1.named_parameters(), fe2.named_parameters()
            ):
                if name1 == name2:
                    # Calculate differences
                    diff = param1.data - param2.data
                    abs_diff = torch.abs(diff)
                    
                    print(f"\nParameter: {name1}")
                    print(f"  Mean absolute difference: {abs_diff.mean().item():.6f}")
                    print(f"  Max absolute difference: {abs_diff.max().item():.6f}")
                    print(f"  % of weights changed significantly: "
                          f"{(abs_diff > 0.01).float().mean().item() * 100:.2f}%")
        
        # Compare policy networks
        if (hasattr(model1, "policy") and hasattr(model1.policy, "mlp_extractor") and
            hasattr(model2, "policy") and hasattr(model2.policy, "mlp_extractor")):
            
            mlp1 = model1.policy.mlp_extractor
            mlp2 = model2.policy.mlp_extractor
            
            print("\n=== POLICY NETWORK COMPARISON ===")
            
            # Compare policy_net
            if hasattr(mlp1, "policy_net") and hasattr(mlp2, "policy_net"):
                for i, ((name1, param1), (name2, param2)) in enumerate(zip(
                    mlp1.policy_net.named_parameters(), mlp2.policy_net.named_parameters()
                )):
                    if name1 == name2 and 'weight' in name1:
                        # Calculate differences
                        diff = param1.data - param2.data
                        abs_diff = torch.abs(diff)
                        
                        print(f"\nPolicy Layer {i//2}: {name1}")
                        print(f"  Mean absolute difference: {abs_diff.mean().item():.6f}")
                        print(f"  Max absolute difference: {abs_diff.max().item():.6f}")
                        print(f"  % of weights changed significantly: "
                              f"{(abs_diff > 0.01).float().mean().item() * 100:.2f}%")
        
        print("\n=== COMPARISON COMPLETE ===")
        
    except Exception as e:
        print(f"Error comparing models: {e}")
        print(traceback.format_exc())
        
class DynamicBatchSizeCallback(BaseCallback):
    """Dynamically adjust batch size based on system resources"""
    def __init__(self, verbose=0):
        super().__init__(verbose)
        self.psutil_available = False
        
        # Check for psutil
        try:
            import psutil
            self.psutil_available = True
        except ImportError:
            logging.warning("psutil not available. Dynamic batch sizing disabled.")
            
    def _on_step(self):
        # Only adjust every 10,000 steps
        if self.n_calls % 10000 == 0 and self.psutil_available:
            try:
                import psutil
                
                # Get current memory usage
                mem = psutil.virtual_memory()
                mem_percent = mem.percent
                
                # Get current batch size
                current_batch_size = self.model.batch_size
                
                # Adjust batch size based on memory usage
                if mem_percent > 85:  # High memory usage
                    new_batch_size = max(64, int(current_batch_size * 0.8))
                    logging.info(f"High memory usage ({mem_percent}%). Reducing batch size: {current_batch_size} -> {new_batch_size}")
                    self.model.batch_size = new_batch_size
                    
                elif mem_percent < 60:  # Low memory usage, can increase
                    new_batch_size = min(512, int(current_batch_size * 1.2))
                    if new_batch_size != current_batch_size:
                        logging.info(f"Low memory usage ({mem_percent}%). Increasing batch size: {current_batch_size} -> {new_batch_size}")
                        self.model.batch_size = new_batch_size
                        
            except Exception as e:
                logging.warning(f"Failed to adjust batch size: {e}")
                
        return True
    
class TrainingPerformanceCallback(BaseCallback):
    """Monitor training performance metrics"""
    def __init__(self, log_dir, monitor_freq=1000, moving_avg_window=10, verbose=0):
        super().__init__(verbose)
        self.log_dir = log_dir
        self.monitor_freq = monitor_freq
        self.writer = None
        self.steps_per_second_history = []
        self.moving_avg_window = moving_avg_window
        self.last_step_time = None
        self.last_step_count = 0
        
    def _init_callback(self):
        from torch.utils.tensorboard import SummaryWriter
        os.makedirs(self.log_dir, exist_ok=True)
        self.writer = SummaryWriter(self.log_dir)
        self.last_step_time = time.time()
        
    def _on_step(self):
        if self.n_calls % self.monitor_freq == 0:
            current_time = time.time()
            elapsed = current_time - self.last_step_time
            steps = self.n_calls - self.last_step_count
            
            if elapsed > 0 and steps > 0:
                steps_per_second = steps / elapsed
                self.steps_per_second_history.append(steps_per_second)
                
                # Keep only the last N entries for moving average
                if len(self.steps_per_second_history) > self.moving_avg_window:
                    self.steps_per_second_history = self.steps_per_second_history[-self.moving_avg_window:]
                
                # Calculate moving average
                avg_steps_per_second = sum(self.steps_per_second_history) / len(self.steps_per_second_history)
                
                # Log to TensorBoard
                self.writer.add_scalar("performance/steps_per_second", steps_per_second, self.n_calls)
                self.writer.add_scalar("performance/avg_steps_per_second", avg_steps_per_second, self.n_calls)
                
                # Estimate time remaining
                if hasattr(self.model, "num_timesteps") and hasattr(self.model, "_total_timesteps"):
                    steps_remaining = self.model._total_timesteps - self.model.num_timesteps
                    if steps_remaining > 0 and avg_steps_per_second > 0:
                        time_remaining = steps_remaining / avg_steps_per_second
                        hours, remainder = divmod(time_remaining, 3600)
                        minutes, seconds = divmod(remainder, 60)
                        
                        time_remaining_str = f"{int(hours)}h {int(minutes)}m {int(seconds)}s"
                        self.writer.add_text("performance/estimated_time_remaining", 
                                            time_remaining_str, self.n_calls)
                        
                        if self.verbose > 0:
                            logging.info(f"Step {self.n_calls}: {steps_per_second:.2f} steps/s, Est. remaining: {time_remaining_str}")
            
            # Reset for next calculation
            self.last_step_time = current_time
            self.last_step_count = self.n_calls
        
        return True
        
    def _on_training_end(self):
        if self.writer is not None:
            self.writer.close()

def record_network_architecture(model, run_id):
    """Record the neural network architecture to a text file"""
    architecture_dir = os.path.join(MODEL_DIR, run_id, "architecture")
    os.makedirs(architecture_dir, exist_ok=True)
    
    try:
        with open(os.path.join(architecture_dir, "network_summary.txt"), "w") as f:
            # Get the policy
            policy = model.policy
            
            # Write basic model info
            f.write(f"Model Type: {type(model).__name__}\n")
            f.write(f"Policy Type: {type(policy).__name__}\n\n")
            
            # Write feature extractor info
            f.write("Feature Extractor:\n")
            f.write(f"  Type: {type(policy.features_extractor).__name__}\n")
            f.write(f"  Output Dimension: {policy.features_extractor.output_dim}\n\n")
            
            # Write policy network info
            f.write("Policy Network:\n")
            for name, module in policy.named_modules():
                if name and not name.startswith('features_extractor'):
                    f.write(f"  {name}: {module}\n")
            
            # Write parameter counts
            total_params = sum(p.numel() for p in policy.parameters())
            trainable_params = sum(p.numel() for p in policy.parameters() if p.requires_grad)
            f.write(f"\nTotal Parameters: {total_params:,}\n")
            f.write(f"Trainable Parameters: {trainable_params:,}\n")
            
        logging.info(f"Network architecture recorded to {architecture_dir}")
    except Exception as e:
        logging.error(f"Failed to record network architecture: {str(e)}")

class StrictTrainingFidelityCallback(BaseCallback):
    """Abort training instead of learning from an engine-contract failure."""

    def __init__(self):
        super().__init__(verbose=0)
        self.checked_steps = 0
        self.signature_histories = None

    def _on_training_start(self):
        env_count = int(getattr(self.training_env, "num_envs", 1))
        self.signature_histories = [[] for _ in range(env_count)]

    def _on_step(self):
        self.checked_steps += 1
        infos = self.locals.get("infos", ()) or ()
        dones = np.asarray(
            self.locals.get("dones", np.zeros(len(infos), dtype=bool)),
            dtype=bool).reshape(-1)
        observations = self.locals.get("new_obs")
        actions = self.locals.get("actions")

        if (self.signature_histories is not None
                and observations is not None and actions is not None):
            for env_index, history in enumerate(self.signature_histories):
                if env_index < len(dones) and dones[env_index]:
                    history.clear()
                    continue
                history.append(rollout_signature(
                    observations, None, actions, env_index))
                if len(history) > 12:
                    del history[:-12]
                period = repeated_short_cycle_period(history)
                if period is not None:
                    info = infos[env_index] if env_index < len(infos) else {}
                    raise RuntimeError(
                        "Strict training detected a non-progressing policy "
                        f"cycle of period {period} in environment {env_index}; "
                        f"actions={np.asarray(actions).reshape(-1).tolist()}, "
                        f"state={info.get('policy_state')}")

        for env_index, info in enumerate(infos):
            detail = training_fidelity_failure(info)
            if detail is not None:
                raise RuntimeError(
                    "Strict training fidelity failure in environment "
                    f"{env_index}: {detail}")
        return True


class RewardComponentsCallback(BaseCallback):
    """Expose environment reward composition in the normal PPO TensorBoard."""

    def __init__(self):
        super().__init__(verbose=0)
        self._terminal_counts = {}
        self._terminal_total = 0
        self._transition_total = 0

    def _on_step(self):
        infos = list(self.locals.get("infos", ()) or ())
        dones = self.locals.get("dones")
        transition_rewards = self.locals.get("rewards")
        if transition_rewards is not None:
            for value in np.asarray(transition_rewards).reshape(-1):
                if np.isfinite(value):
                    numeric = float(value)
                    self.logger.record_mean("reward/total", numeric)
                    self.logger.record_mean("reward/total_abs", abs(numeric))
        self._transition_total += len(infos)
        for index, info in enumerate(infos):
            for name, value in (info.get("reward_components") or {}).items():
                if np.isfinite(value):
                    numeric = float(value)
                    self.logger.record_mean(f"reward/{name}", numeric)
                    self.logger.record_mean(
                        f"reward/{name}_abs", abs(numeric))
                    self.logger.record_mean(
                        f"reward/{name}_nonzero", float(numeric != 0.0))
            for name, value in (info.get("reward_diagnostics") or {}).items():
                if np.isfinite(value):
                    self.logger.record_mean(
                        f"reward_diagnostic/{name}", float(value))
            reason = info.get("terminal_reason")
            is_done = dones is None or index >= len(dones) or bool(dones[index])
            if reason and is_done:
                safe_reason = str(reason).replace(" ", "_").replace("/", "_")
                self._terminal_counts[safe_reason] = (
                    self._terminal_counts.get(safe_reason, 0) + 1)
                self._terminal_total += 1
        denominator = max(1, self._transition_total)
        self.logger.record(
            "terminal/any_count", self._terminal_total)
        self.logger.record(
            "terminal/any_rate", self._terminal_total / denominator)
        for reason, count in sorted(self._terminal_counts.items()):
            self.logger.record(f"terminal/{reason}_count", count)
            self.logger.record(f"terminal/{reason}_rate", count / denominator)
        return True


class CriticDiagnosticsCallback(BaseCallback):
    """Log rollout target scale and value-fit quality before each PPO update."""

    def __init__(self):
        super().__init__(verbose=0)

    def _on_step(self):
        return True

    def _on_rollout_end(self):
        buffer = getattr(self.model, "rollout_buffer", None)
        if buffer is None:
            return
        values = np.asarray(buffer.values, dtype=np.float64).reshape(-1)
        returns = np.asarray(buffer.returns, dtype=np.float64).reshape(-1)
        advantages = np.asarray(
            buffer.advantages, dtype=np.float64).reshape(-1)
        rewards = np.asarray(buffer.rewards, dtype=np.float64).reshape(-1)
        for name, samples in (
                ("reward", rewards),
                ("return", returns),
                ("value", values),
                ("advantage", advantages)):
            finite = samples[np.isfinite(samples)]
            if not finite.size:
                continue
            self.logger.record(f"critic/{name}_mean", float(np.mean(finite)))
            self.logger.record(f"critic/{name}_std", float(np.std(finite)))
            self.logger.record(
                f"critic/{name}_abs_max", float(np.max(np.abs(finite))))
        valid = np.isfinite(values) & np.isfinite(returns)
        if np.any(valid):
            target_variance = float(np.var(returns[valid]))
            if target_variance > 0.0:
                explained = 1.0 - float(np.var(
                    returns[valid] - values[valid])) / target_variance
                self.logger.record(
                    "critic/rollout_explained_variance", explained)


class PhaseTimingCallback(BaseCallback):
    """Separate rollout wall time from the learner/callback gap."""

    def __init__(self):
        super().__init__(verbose=0)
        self.rollout_started_at = None
        self.previous_rollout_ended_at = None

    def _on_step(self):
        return True

    def _on_rollout_start(self):
        now = time.perf_counter()
        if self.previous_rollout_ended_at is not None:
            self.logger.record(
                "performance/learner_and_callbacks_seconds",
                now - self.previous_rollout_ended_at)
        self.rollout_started_at = now

    def _on_rollout_end(self):
        now = time.perf_counter()
        if self.rollout_started_at is not None:
            self.logger.record(
                "performance/rollout_seconds",
                now - self.rollout_started_at)
        self.previous_rollout_ended_at = now


def create_callbacks(eval_env, run_id, args, num_train_envs=1,
                     tb_run_dir=None):
    """Create a comprehensive set of callbacks"""
    # BaseCallback.n_calls counts VecEnv steps, not individual transitions.
    # Keep CLI frequencies expressed in total training timesteps as documented.
    def callback_frequency(timestep_frequency):
        if timestep_frequency <= 0:
            return timestep_frequency
        return max(timestep_frequency // max(num_train_envs, 1), 1)

    run_model_dir = os.path.join(MODEL_DIR, run_id)
    best_model_dir = os.path.join(run_model_dir, 'best_model')
    checkpoint_dir = os.path.join(run_model_dir, 'checkpoints')
    evaluation_log_dir = os.path.join(LOG_DIR, run_id, 'evaluation')
    for path in (best_model_dir, checkpoint_dir, evaluation_log_dir):
        os.makedirs(path, exist_ok=True)

    # Evaluation callback
    eval_callback = MaskableEvalCallback(
        eval_env=eval_env,
        best_model_save_path=best_model_dir,
        log_path=evaluation_log_dir,
        eval_freq=callback_frequency(args.eval_freq),
        deterministic=True,
        n_eval_episodes=getattr(args, "eval_episodes", 20)
    )

    # Checkpoint callback
    checkpoint_callback = CheckpointCallback(
        save_freq=callback_frequency(args.checkpoint_freq),
        save_path=checkpoint_dir,
        name_prefix=f"ppo_mtg_{run_id}"
    )

    # Progress bar callback
    progress_callback = ProgressBarCallback()
    
    # Resource monitoring callback. Keep every TensorBoard stream for one
    # training run under its single run folder ('<run>/system', '<run>/
    # network') so the sidebar groups them instead of interleaving
    # system_logs_*/network_logs_* entries between runs.
    if tb_run_dir is None:
        tb_run_dir = os.path.join(TENSORBOARD_DIR, run_id)
    resource_callback = ResourceMonitorCallback(
        log_dir=os.path.join(tb_run_dir, "system"),
        monitor_freq=callback_frequency(5000)
    )

    callbacks = [eval_callback, checkpoint_callback, progress_callback]
    if args.record_network:
        callbacks.append(NetworkRecordingCallback(
            log_dir=os.path.join(tb_run_dir, "network"),
            record_freq=callback_frequency(args.record_freq)
        ))
    callbacks.append(resource_callback)
    callbacks.append(RewardComponentsCallback())
    callbacks.append(CriticDiagnosticsCallback())
    callbacks.append(PhaseTimingCallback())
    callbacks.append(StrictTrainingFidelityCallback())
    return callbacks

def analyze_model_weights(model_path):
    """
    Analyze the weights of a saved model to help with interpretation
    Args:
        model_path: Path to the saved model
    """
    try:
        # Load the model
        model = MaskablePPO.load(model_path)
        
        print(f"Analyzing model weights for: {model_path}")
        print("\n" + "="*50)
        print("NETWORK STRUCTURE ANALYSIS")
        print("="*50)
        
        # Analyze feature extractor
        if hasattr(model, "policy") and hasattr(model.policy, "features_extractor"):
            fe = model.policy.features_extractor
            print(f"\nFeature Extractor: {type(fe).__name__}")
            print(f"Output Dimension: {fe.output_dim}")
            
            # Analyze extractors
            if hasattr(fe, "extractors"):
                print("\nObservation Extractors:")
                for key, extractor in fe.extractors.items():
                    print(f"  - {key}: {extractor}")
                    
                    # Count parameters
                    param_count = sum(p.numel() for p in extractor.parameters())
                    print(f"    Parameters: {param_count:,}")
                    
                    # Get weight statistics
                    for name, param in extractor.named_parameters():
                        if 'weight' in name:
                            print(f"    {name} stats: mean={param.mean().item():.4f}, std={param.std().item():.4f}")
            
            # Analyze other components
            components = ["phase_embedding", "final_projection", "lstm"]
            for comp_name in components:
                if hasattr(fe, comp_name):
                    comp = getattr(fe, comp_name)
                    print(f"\n{comp_name.replace('_', ' ').title()}: {comp}")
                    
                    # Count parameters
                    param_count = sum(p.numel() for p in comp.parameters())
                    print(f"  Parameters: {param_count:,}")
        
        # Analyze policy network
        if hasattr(model, "policy") and hasattr(model.policy, "mlp_extractor"):
            mlp = model.policy.mlp_extractor
            print("\n" + "="*50)
            print("POLICY NETWORK ANALYSIS")
            print("="*50)
            
            # Policy network
            if hasattr(mlp, "policy_net"):
                print("\nPolicy Network:")
                policy_net = mlp.policy_net
                
                # Count layers
                linear_layers = [m for m in policy_net if isinstance(m, torch.nn.Linear)]
                print(f"  Layers: {len(linear_layers)}")
                
                # Analyze each layer
                for i, layer in enumerate(linear_layers):
                    print(f"\n  Layer {i+1}: {layer}")
                    weights = layer.weight.data
                    
                    # Weight statistics
                    print(f"    Shape: {weights.shape}")
                    print(f"    Parameters: {weights.numel():,}")
                    print(f"    Stats: mean={weights.mean().item():.4f}, std={weights.std().item():.4f}")
                    
                    # Neuron influence analysis
                    neuron_importance = torch.norm(weights, dim=1)
                    top_values, top_indices = torch.topk(neuron_importance, min(5, len(neuron_importance)))
                    
                    print(f"    Top 5 influential neurons:")
                    for idx, (neuron_idx, magnitude) in enumerate(zip(top_indices.tolist(), top_values.tolist())):
                        print(f"      Neuron {neuron_idx}: magnitude={magnitude:.4f}")
            
            # Value network
            if hasattr(mlp, "value_net"):
                print("\nValue Network:")
                value_net = mlp.value_net
                
                # Count layers
                linear_layers = [m for m in value_net if isinstance(m, torch.nn.Linear)]
                print(f"  Layers: {len(linear_layers)}")
                
                # Analyze each layer
                for i, layer in enumerate(linear_layers):
                    print(f"\n  Layer {i+1}: {layer}")
                    weights = layer.weight.data
                    
                    # Weight statistics
                    print(f"    Shape: {weights.shape}")
                    print(f"    Parameters: {weights.numel():,}")
                    print(f"    Stats: mean={weights.mean().item():.4f}, std={weights.std().item():.4f}")
                    
        print("\n" + "="*50)
        print("ANALYSIS COMPLETE")
        print("="*50)
        
    except Exception as e:
        print(f"Error analyzing model: {e}")
        print(traceback.format_exc())
        
def main():
    parser = argparse.ArgumentParser(description="Train an MTG AI agent")
    parser.add_argument("--resume", type=str, help="Path to a model to resume training from")
    parser.add_argument("--run-name", type=str, default=None,
                        help="Short label folded into the run id and TensorBoard "
                             "run name so runs are recognizable at a glance "
                             "(e.g. --run-name lr3e4-crewfix)")
    parser.add_argument("--timesteps", type=int, default=1000000, help="Total timesteps to train")
    parser.add_argument("--eval-freq", type=int, default=10000, help="Evaluation frequency")
    parser.add_argument("--eval-episodes", type=int, default=20,
                        help="Episodes per periodic evaluation")
    parser.add_argument("--checkpoint-freq", type=int, default=50000, help="Checkpoint frequency")
    parser.add_argument("--learning-rate", type=float, default=1e-4, help="Initial learning rate")
    parser.add_argument("--batch-size", type=int, default=512, help="Batch size for training")
    parser.add_argument("--n-steps", type=int, default=2048, help="Number of steps to collect before training")  # Reduced for CPU
    parser.add_argument("--n-envs", type=int, default=0, help="Number of environments to run in parallel (0 = auto)")
    parser.add_argument("--debug", action="store_true", help="Enable additional debugging")
    parser.add_argument("--optimize-hp", action="store_true", help="Run hyperparameter optimization")
    parser.add_argument("--record-network", action="store_true", 
                        help="Enable detailed network recording (weights, gradients)")
    parser.add_argument("--record-freq", type=int, default=5000, 
                        help="Frequency for recording network parameters")
    parser.add_argument("--cpu-only", action="store_true", help="Force CPU training even if GPU is available")
    parser.add_argument("--seed", type=int, default=42,
                        help="Base seed for Python, NumPy, Torch, workers, and evaluation")
    parser.add_argument("--format", type=str, default=DEFAULT_FORMAT_NAME,
                        help="Enforce strict format legality and load the frozen "
                             "formats/<format> card registry and feature schema "
                             f"(default: {DEFAULT_FORMAT_NAME})")
    parser.add_argument("--decks", type=str, default=None,
                        help="Deck corpus directory "
                             "(default: formats/<format>/decks)")
    parser.add_argument("--format-dir", type=str, default=None,
                        help="Explicit frozen format-namespace directory "
                             "(default: formats/<format> when --format is given)")
    args = parser.parse_args()
    if args.resume and args.optimize_hp:
        parser.error("--resume and --optimize-hp cannot be used together")
    if args.timesteps <= 0:
        parser.error("--timesteps must be positive")
    if args.eval_episodes <= 0:
        parser.error("--eval-episodes must be positive")
    maximum_base_seed = (2**32 - 1) - EVALUATION_SEED_OFFSET - 10_000
    if not 0 <= args.seed <= maximum_base_seed:
        parser.error(
            f"--seed must be between 0 and {maximum_base_seed} so worker seeds remain valid")

    configure_runtime_logging(debug=args.debug, worker=False)

    if args.cpu_only:
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
        logging.info("Running in CPU-only mode as requested")

    set_random_seed(args.seed)
    detected_cpus = safe_cpu_count()
    n_cpu_threads = min(10, detected_cpus)
    torch.set_num_threads(n_cpu_threads)
    logging.info(f"PyTorch using {n_cpu_threads} CPU threads")

    runtime = runtime_provenance(cpu_only=args.cpu_only)
    selected_device = runtime["selected_device"]
    if selected_device == "cuda":
        logging.info(
            "Using %s GPU(s): %s",
            len(runtime["cuda_devices"]),
            [device["name"] for device in runtime["cuda_devices"]])
    else:
        logging.info("Using CPU for training")

    for directory in (MODEL_DIR, LOG_DIR, TENSORBOARD_DIR):
        os.makedirs(directory, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_label = re.sub(r"[^A-Za-z0-9._-]+", "-",
                       args.run_name).strip("-_.") if args.run_name else ""
    base_run_id = (f"{VERSION}_{timestamp}_{run_label}" if run_label
                   else f"{VERSION}_{timestamp}")
    run_id = base_run_id
    suffix = 1
    while True:
        run_model_dir = os.path.join(MODEL_DIR, run_id)
        try:
            os.makedirs(run_model_dir, exist_ok=False)
            break
        except FileExistsError:
            run_id = f"{base_run_id}_{suffix}"
            suffix += 1

    # TensorBoard's run list truncates long names, and every run used to share
    # the same VERSION prefix with only the trailing timestamp digits
    # differing. Lead with the distinct part (day-time, then the label), and
    # group this run's streams (train/system/network) under one folder so the
    # sidebar reads e.g. '0713-103012_lr3e4/train' instead of three scattered
    # 'ALPHA_ZERO_MTG_V3.00_...' entries per run.
    tb_run_name = f"{timestamp[4:8]}-{timestamp[9:]}"  # MMDD-HHMMSS
    if run_label:
        tb_run_name += f"_{run_label}"
    if run_id != base_run_id:
        tb_run_name += f"_{suffix - 1}"
    tb_run_dir = os.path.join(TENSORBOARD_DIR, tb_run_name)

    manifest_path = os.path.join(run_model_dir, "training_run.json")
    manifest = {
        "schema_version": TRAINING_MANIFEST_SCHEMA_VERSION,
        "kind": "playersim_training_run",
        "run_id": run_id,
        "project_version": VERSION,
        "status": "initializing",
        "phase": "startup",
        "timestamps": {
            "started_at": utc_timestamp(),
            "updated_at": None,
            "finished_at": None,
            "duration_seconds": None,
        },
        "request": {
            "argv": list(sys.argv),
            "cli": vars(args).copy(),
            "resume_checkpoint": artifact_identity(args.resume) if args.resume else None,
        },
        "source": {
            "git": git_provenance(),
            "requirements": artifact_identity(os.path.join(BASE_DIR, "requirements.txt")),
            "working_tree_patch": capture_working_tree_patch(run_model_dir),
        },
        "runtime": runtime,
        "resolved": {},
        "data": None,
        "lineage": None,
        "paths": {
            "model_directory": os.path.relpath(run_model_dir, BASE_DIR).replace(os.sep, "/"),
            "log_directory": os.path.relpath(
                os.path.join(LOG_DIR, run_id), BASE_DIR).replace(os.sep, "/"),
            "tensorboard_directory": os.path.relpath(
                tb_run_dir, BASE_DIR).replace(os.sep, "/"),
        },
        "artifacts": {},
        "metrics": {},
        "validation": {"status": "not_run"},
        "failure": None,
    }

    def publish_manifest():
        manifest["timestamps"]["updated_at"] = utc_timestamp()
        write_json_atomic(manifest_path, manifest)

    publish_manifest()

    vec_env = None
    eval_env = None
    model = None
    exit_code = 1
    start_time = time.time()
    initial_num_timesteps = 0
    current_phase = "data_loading"
    try:
        manifest["phase"] = current_phase
        publish_manifest()
        logging.info("Loading decks and card database...")
        decks, card_db, decks_dir, lineage = load_training_corpus(
            args.decks, args.format, args.format_dir)
        run_subtype_vocab = tuple(Card.SUBTYPE_VOCAB)
        manifest["lineage"] = lineage
        logging.info(
            "Loaded %s decks with %s unique cards", len(decks), len(card_db))

        current_phase = "hyperparameter_optimization"
        best_params = None
        if args.optimize_hp:
            import psutil
            cpu_count = psutil.cpu_count(logical=True) or detected_cpus
            if cpu_count <= 4:
                n_trials = 10
            elif cpu_count <= 8:
                n_trials = 25
            else:
                n_trials = 50
            logging.info(
                "Running seeded hyperparameter optimization with %s trials",
                n_trials)
            best_params = optimize_hyperparameters(
                n_trials=n_trials, seed=args.seed)
            logging.info(
                "Hyperparameter optimization completed; applying the complete "
                "winning configuration: %s", best_params)

        training_config = build_training_config(args, best_params)
        num_envs = (
            args.n_envs if args.n_envs > 0
            else max(1, min(6, detected_cpus // 2))
        )
        # A single alternating-seat evaluator avoids global random/NumPy stream
        # coupling between multiple environments inside DummyVecEnv.
        eval_env_count = 1
        eval_seed = args.seed + EVALUATION_SEED_OFFSET
        eval_rng = random.Random(eval_seed)
        eval_decks = eval_rng.sample(decks, min(10, len(decks)))
        subproc_start_method = "spawn" if os.name == "nt" else None
        learner_threads = (
            max(2, detected_cpus - num_envs)
            if num_envs > 1 else n_cpu_threads)
        torch.set_num_threads(learner_threads)
        logging.info("Creating %s training environments", num_envs)

        environment_data_dir = os.path.join(
            LOG_DIR, run_id, "environment_data")
        train_storage_dir = os.path.join(environment_data_dir, "train")
        eval_storage_dir = os.path.join(environment_data_dir, "eval")
        manifest["data"] = deck_provenance(decks, card_db, decks_dir=decks_dir)
        manifest["data"]["evaluation_decks"] = [
            (deck.get("name") if isinstance(deck, dict)
             else f"non-dict-deck-{index}")
            for index, deck in enumerate(eval_decks)]
        manifest["resolved"] = {
            "training_config": json_safe(training_config),
            "optimized_parameters": json_safe(best_params),
            "seed": args.seed,
            "train_worker_seeds": [args.seed + index
                                   for index in range(num_envs)],
            "evaluation_seed": eval_seed,
            "evaluation_worker_seeds": [eval_seed + index
                                         for index in range(eval_env_count)],
            "train_environments": num_envs,
            "evaluation_environments": eval_env_count,
            "train_vec_env": (
                "SubprocVecEnv" if num_envs > 1 else "DummyVecEnv"),
            "evaluation_vec_env": "DummyVecEnv",
            "subprocess_start_method": (
                subproc_start_method if num_envs > 1 else None),
            "learner_threads": learner_threads,
            "selected_device": selected_device,
            "alternate_agent_seat": True,
            "opponent_policy": "scripted",
            "callback_frequencies_timesteps": {
                "evaluation": args.eval_freq,
                "checkpoint": args.checkpoint_freq,
                "network_recording": (
                    args.record_freq if args.record_network else None),
            },
            "evaluation_episodes": args.eval_episodes,
        }
        manifest["phase"] = "environment_setup"
        publish_manifest()

        def make_env_factory(idx):
            def _init():
                configure_runtime_logging(
                    debug=args.debug,
                    worker=(multiprocessing.current_process().name != "MainProcess"))
                return make_masked_mtg_env(
                    decks, card_db,
                    os.path.join(train_storage_dir, f"env_{idx}"),
                    agent_is_p1=(idx % 2 == 0),
                    alternate_agent_seat=True,
                    subtype_vocab=run_subtype_vocab,
                    reward_discount=training_config['gamma'],
                    action_reward_scale=training_config[
                        'action_reward_scale'],
                    state_potential_scale=training_config[
                        'state_potential_scale'])
            return _init

        env_fns = [make_env_factory(index) for index in range(num_envs)]
        if num_envs > 1:
            subproc_kwargs = {}
            if subproc_start_method is not None:
                subproc_kwargs["start_method"] = subproc_start_method
            raw_vec_env = SubprocVecEnv(env_fns, **subproc_kwargs)
        else:
            raw_vec_env = DummyVecEnv(env_fns)
        vec_env = VecMonitor(raw_vec_env)
        vec_env.env_method("set_agent_version", run_id)
        if hasattr(vec_env, "seed"):
            assigned_train_seeds = vec_env.seed(args.seed)
            manifest["resolved"]["assigned_train_worker_seeds"] = json_safe(
                assigned_train_seeds)

        def make_eval_env_factory(idx):
            def _init():
                return make_masked_mtg_env(
                    eval_decks, card_db,
                    os.path.join(eval_storage_dir, f"env_{idx}"),
                    agent_is_p1=(idx % 2 == 0),
                    alternate_agent_seat=True,
                    subtype_vocab=run_subtype_vocab,
                    reward_discount=training_config['gamma'],
                    action_reward_scale=training_config[
                        'action_reward_scale'],
                    state_potential_scale=training_config[
                        'state_potential_scale'])
            return _init

        eval_env_fns = [
            make_eval_env_factory(index) for index in range(eval_env_count)]
        eval_env = StrictEvaluationVecEnv(
            VecMonitor(DummyVecEnv(eval_env_fns)))
        eval_env.env_method("set_agent_version", f"{run_id}-eval")
        if hasattr(eval_env, "seed"):
            assigned_eval_seeds = eval_env.seed(eval_seed)
            manifest["resolved"]["assigned_evaluation_worker_seeds"] = json_safe(
                assigned_eval_seeds)

        callbacks = create_callbacks(
            eval_env, run_id, args, num_train_envs=num_envs,
            tb_run_dir=tb_run_dir)

        current_phase = "model_setup"
        manifest["phase"] = current_phase
        publish_manifest()
        if args.resume:
            model = MaskablePPO.load(
                args.resume,
                env=vec_env,
                tensorboard_log=tb_run_dir,
                seed=args.seed,
                device=selected_device)
            logging.info(f"Resuming training from {args.resume}")
        else:
            model = create_training_model(
                vec_env, training_config, args.seed, selected_device,
                tb_run_dir)
        if hasattr(model, "set_random_seed"):
            model.set_random_seed(args.seed)
        initial_num_timesteps = int(getattr(model, "num_timesteps", 0))
        manifest["resolved"]["model_device"] = str(
            getattr(model, "device", selected_device))

        current_phase = "training"
        manifest["status"] = "running"
        manifest["phase"] = current_phase
        manifest["timestamps"]["training_started_at"] = utc_timestamp()
        publish_manifest()
        if selected_device == "cuda":
            torch.cuda.reset_peak_memory_stats()
        logging.info(f"Starting training run: {run_id}")
        # SB3 compares the outer wrapper classes and warns because training is
        # VecMonitor while evaluation adds StrictEvaluationVecEnv around its
        # VecMonitor. Their spaces are deliberately identical; suppress only
        # this known structural false positive, not general training warnings.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"Training and eval env are not of the same type.*",
                category=UserWarning,
                module=r"stable_baselines3\.common\.callbacks")
            model.learn(
                total_timesteps=args.timesteps,
                callback=callbacks,
                tb_log_name="train",
                reset_num_timesteps=not args.resume
            )

        training_duration = time.time() - start_time
        hours, remainder = divmod(training_duration, 3600)
        minutes, seconds = divmod(remainder, 60)
        logging.info(f"Training completed in {int(hours)}h {int(minutes)}m {int(seconds)}s")

        current_phase = "artifact_validation"
        manifest["phase"] = current_phase
        logging.info("Recording neural network architecture...")
        record_network_architecture(model, run_id)

        if hasattr(model, "policy") and hasattr(
                model.policy, "features_extractor"):
            feature_extractor_path = os.path.join(
                run_model_dir, "feature_extractor.pth")
            torch.save(
                model.policy.features_extractor.state_dict(),
                feature_extractor_path)
            logging.info(
                f"Saved feature extractor to {feature_extractor_path}")

        pending_model_path = os.path.join(run_model_dir, "pending_model")
        final_model_path = os.path.join(run_model_dir, "final_model")
        logging.info("Saving pending model to %s", pending_model_path)
        model.save(pending_model_path)
        pending_actual_path = resolve_artifact_path(pending_model_path)
        if pending_actual_path is not None:
            manifest["validation"] = validate_training_checkpoint(
                pending_model_path, eval_env, device=selected_device,
                seed=eval_seed)
            final_actual_path = (
                f"{final_model_path}.zip"
                if pending_actual_path.lower().endswith(".zip")
                else final_model_path)
            os.replace(pending_actual_path, final_actual_path)
        else:
            # Test doubles may record save calls without creating a file. Real
            # SB3 models always take the validated atomic-publish branch above.
            model.save(final_model_path)
            manifest["validation"] = {
                "status": "skipped",
                "reason": "model test double did not create a checkpoint",
            }

        actual_num_timesteps = int(getattr(
            model, "num_timesteps", initial_num_timesteps + args.timesteps))
        added_timesteps = max(0, actual_num_timesteps - initial_num_timesteps)
        manifest["metrics"] = {
            "requested_added_timesteps": args.timesteps,
            "initial_timesteps": initial_num_timesteps,
            "final_timesteps": actual_num_timesteps,
            "actual_added_timesteps": added_timesteps,
            "duration_seconds": training_duration,
            "transitions_per_second": (
                added_timesteps / training_duration if training_duration > 0 else None),
            "cuda_peak_allocated_bytes": (
                torch.cuda.max_memory_allocated() if selected_device == "cuda" else 0),
            "cuda_peak_reserved_bytes": (
                torch.cuda.max_memory_reserved() if selected_device == "cuda" else 0),
            "evaluation": evaluation_history_summary(run_id),
        }
        manifest["status"] = "complete"
        manifest["phase"] = "complete"
        manifest["timestamps"]["finished_at"] = utc_timestamp()
        manifest["timestamps"]["duration_seconds"] = training_duration
        manifest["artifacts"] = training_artifacts(run_model_dir, run_id)
        publish_manifest()
        exit_code = 0

    except (Exception, KeyboardInterrupt) as e:
        was_interrupted = isinstance(e, KeyboardInterrupt)
        if was_interrupted:
            logging.warning(
                "Training interrupted by user; preserving an incomplete checkpoint.")
        else:
            logging.error(f"Training error: {str(e)}")
        failure_traceback = traceback.format_exc()
        if was_interrupted:
            logging.debug(failure_traceback)
        else:
            logging.error(failure_traceback)
        failed_model_save_error = None
        if model is not None:
            failed_model_path = os.path.join(run_model_dir, "failed_model")
            try:
                pending_or_final = (
                    resolve_artifact_path(os.path.join(run_model_dir, "pending_model"))
                    or resolve_artifact_path(os.path.join(run_model_dir, "final_model")))
                if pending_or_final is not None:
                    failed_actual_path = (
                        f"{failed_model_path}.zip"
                        if pending_or_final.lower().endswith(".zip")
                        else failed_model_path)
                    os.replace(pending_or_final, failed_actual_path)
                else:
                    logging.info(
                        f"Saving incomplete model to {failed_model_path}")
                    model.save(failed_model_path)
            except Exception as save_error:
                failed_model_save_error = str(save_error)
                logging.error(
                    f"Could not save incomplete model: {save_error}")
        duration = time.time() - start_time
        manifest["status"] = "failed"
        manifest["phase"] = current_phase
        manifest["timestamps"]["finished_at"] = utc_timestamp()
        manifest["timestamps"]["duration_seconds"] = duration
        manifest["artifacts"] = training_artifacts(run_model_dir, run_id)
        manifest["failure"] = {
            "type": type(e).__name__,
            "message": str(e),
            "phase": current_phase,
            "traceback": failure_traceback,
            "failed_model_save_error": failed_model_save_error,
        }
        try:
            publish_manifest()
        except Exception as manifest_error:
            logging.error("Could not publish failure manifest: %s", manifest_error)
        if was_interrupted:
            exit_code = 130
    finally:
        if vec_env is not None:
            vec_env.close()
        if eval_env is not None:
            eval_env.close()

    if exit_code == 0:
        logging.info(f"Training run {run_id} completed")
    elif exit_code == 130:
        logging.warning(f"Training run {run_id} was interrupted")
    else:
        logging.error(f"Training run {run_id} failed")
    return exit_code
    
if __name__ == "__main__":
    # Code to run only when the script is executed directly
    sys.exit(main())
