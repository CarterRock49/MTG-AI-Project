import os
import json
import gzip
import hashlib
import math
import queue
import re
import platform
import shutil
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
from copy import deepcopy
from collections import deque
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
    CloudpickleWrapper, DummyVecEnv, SubprocVecEnv, VecEnvWrapper, VecMonitor)
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
from Playersim.curriculum import (
    derive_matchup_seed, resolve_curriculum,
)
from Playersim.observation_schema import SEMANTIC_IDENTITY_FIELDS
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
        self.semantic_identity_fields = tuple(
            key for key in SEMANTIC_IDENTITY_FIELDS
            if key in observation_space.spaces)
        identity_highs = {
            int(np.asarray(observation_space.spaces[key].high).max())
            for key in self.semantic_identity_fields
        }
        if len(identity_highs) != 1:
            raise ValueError(
                "Semantic identity fields must share one frozen vocabulary")
        identity_vocabulary_size = (
            identity_highs.pop() + 1 if identity_highs else 2)
        self.semantic_identity_embedding = torch.nn.Embedding(
            identity_vocabulary_size, 32, padding_idx=0)
        
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
        self.preprocessing_dim = 512  # Intermediate dimension
        self.final_projection = torch.nn.Sequential(
            torch.nn.Linear(self.preprocessing_dim, self.output_dim),
            torch.nn.ReLU()
        )
        
        # Process each observation type separately
        for key, subspace in observation_space.spaces.items():
            # phase has a dedicated embedding; action_mask is consumed by
            # MaskablePPO; target_card_ids are unstable runtime occurrence
            # handles used only to verify the public target-page protocol.
            if key in {"phase", "action_mask", "target_card_ids"}:
                continue

            if key in SEMANTIC_IDENTITY_FIELDS:
                if len(subspace.shape) != 1:
                    raise ValueError(
                        f"Semantic identity field '{key}' must be rank 1, "
                        f"got shape={subspace.shape}")
                high = np.asarray(subspace.high)
                if not np.all(np.isfinite(high)):
                    raise ValueError(
                        f"Semantic identity field '{key}' has no finite bound")
                merged_dim += int(np.prod(subspace.shape)) * \
                    self.semantic_identity_embedding.embedding_dim
                continue
                
            if len(subspace.shape) == 1:
                # 1D vector observations (counts, flags, etc.)
                n_input = int(np.prod(subspace.shape))
                self.extractors[key] = torch.nn.Sequential(
                    torch.nn.Linear(n_input, 64),
                    torch.nn.ReLU(),
                    torch.nn.Linear(64, 128)
                )
                merged_dim += 128

            elif len(subspace.shape) == 2:
                # 2D observations like battlefield and hand
                n_cards, card_dim = subspace.shape
                self.extractors[key] = torch.nn.Sequential(
                    torch.nn.Linear(card_dim, 256),
                    torch.nn.ReLU(),
                    torch.nn.Linear(256, 128),
                    torch.nn.ReLU()
                )
                merged_dim += n_cards * 128

            elif len(subspace.shape) == 3:
                # Preserve the primary object axis while flattening each
                # object's trailing feature grid.  In particular,
                # ability_recommendations is [permanent, ability,
                # (recommend, confidence)]; the old 1D/2D-only dispatch
                # silently omitted it from the policy input.
                n_objects = subspace.shape[0]
                object_dim = int(np.prod(subspace.shape[1:]))
                self.extractors[key] = torch.nn.Sequential(
                    torch.nn.Flatten(start_dim=2),
                    torch.nn.Linear(object_dim, 256),
                    torch.nn.ReLU(),
                    torch.nn.Linear(256, 128),
                    torch.nn.ReLU()
                )
                merged_dim += n_objects * 128

            else:
                raise ValueError(
                    f"Unsupported observation rank for '{key}': "
                    f"shape={subspace.shape}")
        
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
        typical magnitudes (P/T and combat damage saturate at 1e6). Feeding
        those raw into ``Linear`` layers let a single
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

        # Stable canonical identities are categorical.  Passing registry
        # indices through symlog/Linear would incorrectly impose ordinal
        # distance (card 100 is not semantically closer to 101 than to 4000).
        embedding = self.semantic_identity_embedding
        for key in self.semantic_identity_fields:
            if key in observations:
                identity_tensor = observations[key].long().clamp(
                    0, embedding.num_embeddings - 1)
                encoded_tensor_list.append(embedding(identity_tensor))

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


def attach_run_log(log_directory, *, debug=False):
    """Attach a dedicated, complete runtime log to one training run."""
    os.makedirs(log_directory, exist_ok=True)
    handler = logging.FileHandler(
        os.path.join(log_directory, "training.log"),
        encoding="utf-8", errors="replace")
    handler.setLevel(logging.DEBUG if debug else logging.INFO)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(processName)s - %(name)s - "
        "%(levelname)s - %(message)s"))
    logging.getLogger().addHandler(handler)
    return handler

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
DEFAULT_TRAINING_SEED = 20_260_715
DEFAULT_EVALUATION_SEED = 21_260_715
DEFAULT_TRAINING_ENVIRONMENTS = 8

# A named canary is an experiment contract, not merely a convenient collection
# of defaults.  Supplying --canary-config makes launch fail closed if a CLI,
# lineage, reward, curriculum, or evaluation-suite input drifts.
ROUND_7_92_CANARY = {
    "id": "round-7.92",
    "cli": {
        "timesteps": 1_000_000,
        "eval_freq": 100_000,
        "eval_episodes": 64,
        "checkpoint_freq": 50_000,
        "learning_rate": 2e-4,
        "batch_size": 256,
        "n_steps": 1024,
        "n_envs": DEFAULT_TRAINING_ENVIRONMENTS,
        "seed": DEFAULT_TRAINING_SEED,
        "eval_seed": DEFAULT_EVALUATION_SEED,
        "curriculum": "combat-v5",
        "format": DEFAULT_FORMAT_NAME,
        "cpu_only": False,
    },
    "training_config": {
        "learning_rate": 2e-4,
        "n_steps": 1024,
        "batch_size": 256,
        "reward_contract_version": "discounted-state-potential-v6",
        "gamma": 0.999,
        "gae_lambda": 0.98,
        "clip_range": 0.2,
        "clip_range_vf": 0.2,
        "ent_coef": 0.01,
        "vf_coef": 0.25,
        "target_kl": 0.02,
        "net_arch": {
            "pi": [512, 256, 128],
            "vf": [512, 256, 128],
        },
        "n_epochs": 5,
        "max_grad_norm": 0.5,
        "activation_fn": "torch.nn.modules.activation.ReLU",
        "action_reward_scale": 0.0,
        "state_potential_scale": 0.4,
    },
    "lineage": {
        "observation_schema_version": 3,
        "observation_schema_sha256": (
            "6e29a94e3443881681afd794185f061133f24ff72350a7df27f48524f00d4137"),
        "card_registry_sha256": (
            "c1c7248db35957a43b0068c1c790dce2e615f0b349eb15967fb64001ef2351bb"),
        "feature_schema_sha256": (
            "4a0bf0357ae8f9b9b647e4cffa81ef512a36bfcc676fc63b68f7b9a58b99f2fb"),
        "corpus_sha256": (
            "26fc8d70005e25f43bc2f6e2e557274ee7b0c752d5e6e9addf7a104aba7cd89e"),
        "evaluation_schedule_sha256": (
            "f5aa91235bade4a49db923577542032dfcb04a2db2f6fae180f6083072755763"),
    },
    "runtime": {
        "curriculum_sha256": (
            "10a22d4a539a017673e484fc5fcfd3a2ff9f70a1892bcfb8f0bf06948f77f0bb"),
        "feature_output_dim": 1024,
        "selected_device": "cuda",
    },
}
CANARY_CONFIGS = {ROUND_7_92_CANARY["id"]: ROUND_7_92_CANARY}


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


def write_gzip_json_atomic(path, payload):
    """Atomically publish deterministic, compact gzip JSON."""
    serialized = json.dumps(
        json_safe(payload), ensure_ascii=False, allow_nan=False,
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8") + b"\n"
    # A zero timestamp keeps the gzip header stable across repeated writes of
    # the same evaluation payload.  Sidecar identity hashes the exact bytes
    # consumed by the viewer, not a second uncompressed representation.
    compressed = gzip.compress(serialized, compresslevel=6, mtime=0)
    write_bytes_atomic(path, compressed)


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


def build_fixed_evaluation_schedule(decks, n_eval_episodes, seed):
    """Build reproducible, exposure-balanced periodic evaluation pairs.

    Matchups are generated in seeded derangement rounds.  Every complete round
    uses each deck exactly once as the learned deck and once as the opponent,
    with no mirror matches.  Successive rounds use distinct opponents until
    every ordered matchup has appeared.  Each matchup is then played from both
    physical seats with the same seed.  The episode count must be even so no
    learned deck receives an unmatched physical-seat case.
    """
    episode_count = int(n_eval_episodes)
    if episode_count <= 0:
        raise ValueError("n_eval_episodes must be positive")
    if episode_count % 2:
        raise ValueError(
            "n_eval_episodes must be even for paired-seat evaluation")

    deck_names = sorted({
        str(deck.get("name"))
        for deck in decks
        if isinstance(deck, dict) and deck.get("name")
    }, key=str.casefold)
    if not deck_names:
        raise ValueError(
            "Fixed evaluation requires decks with stable non-empty names")
    if len(deck_names) < 2:
        raise ValueError(
            "Fixed evaluation requires at least two distinct decks so mirror "
            "matches can be excluded")

    rng = random.Random(int(seed))
    pair_count = episode_count // 2
    matchups = []
    while len(matchups) < pair_count:
        # A cycle of all non-zero offsets covers every ordered non-mirror
        # matchup exactly once.  Truncating a round still gives unique learned
        # and opponent decks, so both exposure counts differ by at most one.
        deck_order = list(deck_names)
        rng.shuffle(deck_order)
        offsets = list(range(1, len(deck_order)))
        rng.shuffle(offsets)
        for offset in offsets:
            matchup_round = [
                (agent_deck,
                 deck_order[(agent_index + offset) % len(deck_order)])
                for agent_index, agent_deck in enumerate(deck_order)
            ]
            rng.shuffle(matchup_round)
            remaining = pair_count - len(matchups)
            matchups.extend(matchup_round[:remaining])
            if len(matchups) == pair_count:
                break

    schedule = []
    for agent_deck, opponent_deck in matchups:
        pair_seed = rng.randrange(0, 2**32)
        schedule.extend((
            {
                "seed": pair_seed,
                "p1_deck": agent_deck,
                "p2_deck": opponent_deck,
                "agent_is_p1": True,
                "opponent_profile": "scripted",
            },
            {
                "seed": pair_seed,
                "p1_deck": opponent_deck,
                "p2_deck": agent_deck,
                "agent_is_p1": False,
                "opponent_profile": "scripted",
            },
        ))
    return schedule[:episode_count]


def evaluation_schedule_sha256(schedule):
    """Hash a fixed evaluation schedule independent of JSON whitespace."""
    return configuration_sha256(list(schedule))


def configuration_sha256(value):
    """Hash a resolved configuration tree independent of JSON whitespace."""
    payload = json.dumps(
        json_safe(value), sort_keys=True,
        separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _configuration_values_match(actual, expected):
    if isinstance(expected, float):
        try:
            return math.isclose(
                float(actual), expected, rel_tol=0.0, abs_tol=1e-12)
        except (TypeError, ValueError, OverflowError):
            return False
    return actual == expected


def validate_canary_cli(args):
    """Fail closed when a named experiment's requested inputs drift."""
    canary_id = getattr(args, "canary_config", None)
    if not canary_id:
        return None
    config = CANARY_CONFIGS.get(str(canary_id))
    if config is None:
        raise ValueError(f"Unknown canary configuration: {canary_id}")
    mismatches = []
    for key, expected in config["cli"].items():
        actual = getattr(args, key, None)
        if not _configuration_values_match(actual, expected):
            mismatches.append(f"{key}={actual!r} (expected {expected!r})")
    if getattr(args, "resume", None):
        mismatches.append("resume must be unset")
    if getattr(args, "optimize_hp", False):
        mismatches.append("optimize_hp must be false")
    if mismatches:
        raise ValueError(
            f"Canary {canary_id} launch contract mismatch: "
            + "; ".join(mismatches))
    return deepcopy(config)


def validate_canary_runtime(config, *, lineage, training_config, curriculum,
                             schedule_sha256, num_envs, selected_device):
    """Validate resolved code/data identities after the corpus is loaded."""
    if config is None:
        return
    expected_training = json_safe(config["training_config"])
    expected_lineage = config["lineage"]
    expected_runtime = config["runtime"]
    actual_training = json_safe(training_config)
    actual = dict(actual_training)
    actual.update({
        "observation_schema_version":
            AlphaZeroMTGEnv.OBSERVATION_SCHEMA_VERSION,
        "observation_schema_sha256":
            AlphaZeroMTGEnv.OBSERVATION_SCHEMA_SHA256,
        "card_registry_sha256":
            (lineage.get("card_registry") or {}).get("sha256"),
        "feature_schema_sha256":
            (lineage.get("feature_schema") or {}).get("sha256"),
        "corpus_sha256": (lineage.get("corpus") or {}).get("sha256"),
        "evaluation_schedule_sha256": schedule_sha256,
        "curriculum": (curriculum or {}).get("id"),
        "curriculum_sha256": configuration_sha256(curriculum),
        "num_envs": int(num_envs),
        "feature_output_dim": FEATURE_OUTPUT_DIM,
        "selected_device": str(selected_device),
    })
    expected = {
        **expected_training,
        **expected_lineage,
        **expected_runtime,
        "curriculum": config["cli"]["curriculum"],
        "num_envs": config["cli"]["n_envs"],
    }
    mismatches = [
        f"{key}={actual.get(key)!r} (expected {value!r})"
        for key, value in expected.items()
        if not _configuration_values_match(actual.get(key), value)
    ]
    missing_training = sorted(set(expected_training) - set(actual_training))
    unexpected_training = sorted(set(actual_training) - set(expected_training))
    if missing_training:
        mismatches.append(f"training_config missing keys={missing_training!r}")
    if unexpected_training:
        mismatches.append(
            f"training_config unexpected keys={unexpected_training!r}")
    if mismatches:
        raise RuntimeError(
            f"Canary {config['id']} resolved contract mismatch: "
            + "; ".join(mismatches))


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
    """Persist tracked changes and untracked files as one reproducible patch."""
    patch_chunks = []
    try:
        tracked = subprocess.run(
            ["git", "-C", BASE_DIR, "diff", "--binary", "HEAD"],
            capture_output=True, timeout=15, check=False)
        untracked = subprocess.run(
            ["git", "-C", BASE_DIR, "ls-files", "--others",
             "--exclude-standard", "-z"],
            capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if tracked.returncode != 0 or untracked.returncode != 0:
        return None
    if tracked.stdout:
        patch_chunks.append(tracked.stdout)

    # ``git diff HEAD`` omits untracked source entirely.  Generate normal
    # binary-capable new-file patches without mutating the index so a launch
    # from a dirty tree can still be reconstructed exactly.
    for encoded_path in untracked.stdout.split(b"\0"):
        if not encoded_path:
            continue
        relative_path = os.fsdecode(encoded_path)
        try:
            addition = subprocess.run(
                ["git", "-C", BASE_DIR, "diff", "--binary", "--no-index",
                 "--", "/dev/null", relative_path],
                capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        # --no-index uses 1 to mean "files differ".
        if addition.returncode not in (0, 1):
            return None
        if addition.returncode == 1 and not addition.stdout:
            return None
        if addition.stdout:
            patch_chunks.append(addition.stdout)

    if not patch_chunks:
        return None
    patch_payload = b"".join(
        chunk if chunk.endswith(b"\n") else chunk + b"\n"
        for chunk in patch_chunks)
    patch_path = os.path.join(run_model_dir, "source_worktree.patch")
    write_bytes_atomic(patch_path, patch_payload)
    return artifact_identity(patch_path)


def validate_resume_lineage(checkpoint_path, requested_curriculum):
    """Reject checkpoints that cannot safely continue the requested lineage."""
    checkpoint = resolve_artifact_path(checkpoint_path)
    if checkpoint is None:
        raise ValueError(f"Resume checkpoint does not exist: {checkpoint_path}")

    manifest_path = None
    cursor = os.path.dirname(os.path.abspath(checkpoint))
    for _ in range(8):
        candidate = os.path.join(cursor, "training_run.json")
        if os.path.isfile(candidate):
            manifest_path = candidate
            break
        parent = os.path.dirname(cursor)
        if parent == cursor:
            break
        cursor = parent
    if manifest_path is None:
        raise ValueError(
            "Resume checkpoint has no companion training_run.json; its reward "
            "and observation lineage cannot be verified")

    try:
        with open(manifest_path, encoding="utf-8") as handle:
            source_manifest = json.load(handle)
    except (OSError, ValueError) as error:
        raise ValueError(
            f"Could not read resume manifest {manifest_path}: {error}") from error
    if source_manifest.get("kind") != "playersim_training_run":
        raise ValueError("Resume manifest has an unknown artifact kind")

    resolved = source_manifest.get("resolved") or {}
    training_config = resolved.get("training_config") or {}
    reward_contract = training_config.get("reward_contract_version")
    if reward_contract != AlphaZeroMTGEnv.REWARD_CONTRACT_VERSION:
        raise ValueError(
            "Resume checkpoint uses reward contract "
            f"{reward_contract!r}; {AlphaZeroMTGEnv.REWARD_CONTRACT_VERSION!r} "
            "is required")
    schema_version = resolved.get("observation_schema_version")
    schema_sha256 = resolved.get("observation_schema_sha256")
    if (schema_version != AlphaZeroMTGEnv.OBSERVATION_SCHEMA_VERSION
            or schema_sha256 != AlphaZeroMTGEnv.OBSERVATION_SCHEMA_SHA256):
        raise ValueError(
            "Resume checkpoint does not match the current Observation "
            f"v{AlphaZeroMTGEnv.OBSERVATION_SCHEMA_VERSION} version/hash")

    requested_name = None if requested_curriculum in (None, "none") \
        else str(requested_curriculum)
    recorded_curriculum = resolved.get("curriculum")
    recorded_name = (
        recorded_curriculum.get("id")
        if isinstance(recorded_curriculum, dict) else None)
    if recorded_name != requested_name:
        raise ValueError(
            "Resume curriculum does not match its source run: "
            f"recorded={recorded_name!r}, requested={requested_name!r}")
    if requested_name is not None:
        raise ValueError(
            "Curriculum resume is disabled until per-worker scheduler counters "
            "are checkpointed; start this Round 7.89 lineage fresh")

    return {
        "run_id": source_manifest.get("run_id"),
        "manifest": artifact_identity(manifest_path),
        "reward_contract_version": reward_contract,
        "observation_schema_version": schema_version,
        "observation_schema_sha256": schema_sha256,
        "curriculum": recorded_name,
    }


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
        "interrupted_model": artifact_identity(os.path.join(
            run_model_dir, "interrupted_model")),
        "failed_model": artifact_identity(os.path.join(run_model_dir, "failed_model")),
        "best_model": artifact_identity(
            os.path.join(run_model_dir, "best_model", "best_model")),
        "evaluation_history": (
            artifact_identity(os.path.join(
                LOG_DIR, run_id, "evaluation", "evaluations.json"))
            or artifact_identity(os.path.join(
                LOG_DIR, run_id, "evaluation", "evaluations.npz"))),
        "feature_extractor": artifact_identity(
            os.path.join(run_model_dir, "feature_extractor.pth")),
        "network_summary": artifact_identity(os.path.join(
            run_model_dir, "architecture", "network_summary.txt")),
        "runtime_log": artifact_identity(os.path.join(
            LOG_DIR, run_id, "training.log")),
        "checkpoints": checkpoints,
    }


def evaluation_history_summary(run_id):
    """Return a manifest-safe summary of checkpoint-attributable evaluation."""
    json_path = os.path.join(
        LOG_DIR, run_id, "evaluation", "evaluations.json")
    if os.path.isfile(json_path):
        try:
            with open(json_path, encoding="utf-8") as handle:
                history = json.load(handle)
            evaluations = list(history.get("evaluations") or ())
        except (OSError, TypeError, ValueError) as error:
            return {"status": "unreadable", "error": str(error)}
        if not evaluations:
            return {
                "status": "not_run",
                "evaluation_points": 0,
                "schedule_sha256": history.get("schedule_sha256"),
                "episodes_per_evaluation": len(
                    history.get("fixed_schedule") or ()),
                "minimum_qualification_score": history.get(
                    "minimum_qualification_score"),
                "qualification_rule": history.get("qualification_rule"),
                "skipped_evaluations": len(
                    history.get("skipped_evaluations") or ()),
                "cancelled_evaluations": len(
                    history.get("cancelled_evaluations") or ()),
            }
        summaries = [item.get("summary") or {} for item in evaluations]
        promoted = [item for item in evaluations if item.get("promoted")]
        qualified = [item for item in evaluations if item.get("qualified")]
        return {
            "status": (
                "qualified" if qualified else "evaluated_unqualified"),
            "qualified": bool(qualified),
            "evaluation_points": len(evaluations),
            "timesteps": [int(item["timesteps"]) for item in evaluations],
            "episodes_per_evaluation": len(
                history.get("fixed_schedule") or ()),
            "schedule_sha256": history.get("schedule_sha256"),
            "checkpoint_sha256": [
                item.get("checkpoint_sha256") for item in evaluations],
            "mean_rewards": [
                float(summary.get("mean_reward", 0.0))
                for summary in summaries],
            "mean_episode_lengths": [
                float(summary.get("mean_ep_length", 0.0))
                for summary in summaries],
            "decisive_wins": [
                int(summary.get("decisive_wins", 0))
                for summary in summaries],
            "decisive_losses": [
                int(summary.get("decisive_losses", 0))
                for summary in summaries],
            "timeouts": [
                int(summary.get("timeouts", 0))
                for summary in summaries],
            "promotion_keys": [
                item.get("promotion_key") for item in evaluations],
            "qualification_scores": [
                float(item.get("qualification_score", 0.0))
                for item in evaluations],
            "qualification_lower_bounds": [
                float((item.get("qualification_interval") or
                       (item.get("summary") or {}).get(
                           "qualification_interval") or {}).get(
                               "lower_bound", 0.0))
                for item in evaluations],
            "qualification_upper_bounds": [
                float((item.get("qualification_interval") or
                       (item.get("summary") or {}).get(
                           "qualification_interval") or {}).get(
                               "upper_bound", 1.0))
                for item in evaluations],
            "minimum_qualification_score": history.get(
                "minimum_qualification_score"),
            "qualification_rule": history.get("qualification_rule"),
            "qualified_evaluation_points": len(qualified),
            "skipped_evaluations": len(
                history.get("skipped_evaluations") or ()),
            "cancelled_evaluations": len(
                history.get("cancelled_evaluations") or ()),
            "best_candidate_timestep": history.get(
                "best_candidate_timestep"),
            "best_timestep": (
                int(promoted[-1]["timesteps"]) if promoted else None),
        }

    # Compatibility with historical synchronous EvalCallback runs.
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

    def has_issue(value):
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float, np.number)):
            try:
                return not np.isfinite(value) or float(value) != 0.0
            except (TypeError, ValueError, OverflowError):
                return True
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, dict):
            return any(has_issue(item) for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return any(has_issue(item) for item in value)
        return bool(value)

    fidelity = info.get("fidelity")
    fidelity_issues = {
        str(key): value for key, value in fidelity.items()
        if has_issue(value)
    } if isinstance(fidelity, dict) else {}
    if not severe_flags and not fidelity_issues:
        return None
    return {
        "message": (info.get("error_message")
                    or info.get("invalid_action_reason")),
        "engine": severe_flags,
        "fidelity": fidelity_issues,
    }


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
# Round 7.76: widened 512 -> 1024 (with the per-key extractor widths and the
# default head doubling to 'large'). Training is env-bound and the learner
# uses ~0.2 of 8 GB VRAM, so extra capacity is nearly free in wall time; it
# buys sample efficiency, which is what matters when every sample costs
# CPU-simulated game steps. Width changes start a new checkpoint lineage.
FEATURE_OUTPUT_DIM = 1024

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
        'gamma': 0.999,
        'gae_lambda': 0.98,
        'clip_range': 0.2,
        'clip_range_vf': 0.2,
        'ent_coef': 0.01,
        'vf_coef': 0.25,
        'target_kl': 0.02,
        'net_arch': NETWORK_ARCHITECTURES['large'],
        'n_epochs': 5,
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
                        strategy_memory_enabled=False,
                        adaptive_decision_history_enabled=False,
                        reward_discount=AlphaZeroMTGEnv.DEFAULT_REWARD_DISCOUNT,
                        action_reward_scale=
                            AlphaZeroMTGEnv.DEFAULT_ACTION_REWARD_SCALE,
                        state_potential_scale=
                            AlphaZeroMTGEnv.DEFAULT_STATE_POTENTIAL_SCALE,
                        curriculum=None, opponent_profile="scripted",
                        matchup_seed=None,
                        stats_persistence_interval_games=10):
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
            strategy_memory_enabled=strategy_memory_enabled,
            adaptive_decision_history_enabled=
                adaptive_decision_history_enabled,
            reward_discount=reward_discount,
            action_reward_scale=action_reward_scale,
            state_potential_scale=state_potential_scale,
            curriculum=curriculum,
            opponent_profile=opponent_profile,
            matchup_seed=matchup_seed,
            stats_persistence_interval_games=
                stats_persistence_interval_games,
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
            reward_discount=gamma,
            stats_persistence_interval_games=10)

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
            reward_discount=gamma,
            adaptive_decision_history_enabled=False,
            stats_persistence_interval_games=1)

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
        self._unhealthy_rollouts = 0
        self._terminal_counts = {}
        self._terminal_total = 0
        self._transition_total = 0
        self._outcome_counts = {
            "win": 0, "loss": 0, "draw": 0, "unknown": 0,
            "timeout": 0, "decisive_win": 0, "decisive_loss": 0,
        }
        self._terminal_reward_mismatches = 0
        self._profile_outcomes = {}
        self._stage_outcomes = {}

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
            action_mask = info.get("action_mask")
            if action_mask is not None:
                self.logger.record_mean(
                    "policy/valid_action_count",
                    float(np.asarray(action_mask, dtype=bool).sum()))
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
            if is_done:
                safe_reason = str(reason or "unknown").replace(
                    " ", "_").replace("/", "_")
                self._terminal_counts[safe_reason] = (
                    self._terminal_counts.get(safe_reason, 0) + 1)
                self._terminal_total += 1
                try:
                    result = canonical_evaluation_game_result(
                        info.get("game_result"))
                except RuntimeError:
                    result = "unknown"
                self._outcome_counts[result] += 1
                is_timeout = safe_reason == "turn_limit"
                if is_timeout:
                    self._outcome_counts["timeout"] += 1
                elif result in {"win", "loss"}:
                    self._outcome_counts[f"decisive_{result}"] += 1
                for buckets, label in (
                        (self._profile_outcomes,
                         info.get("opponent_profile") or "unknown"),
                        (self._stage_outcomes,
                         info.get("curriculum_stage") or "none")):
                    safe_label = re.sub(
                        r"[^a-zA-Z0-9_.-]+", "_", str(label))
                    bucket = buckets.setdefault(safe_label, {
                        "episodes": 0, "decisive_win": 0,
                        "decisive_loss": 0, "timeout": 0,
                    })
                    bucket["episodes"] += 1
                    if is_timeout:
                        bucket["timeout"] += 1
                    elif result in {"win", "loss"}:
                        bucket[f"decisive_{result}"] += 1

                terminal_component = (info.get("reward_components") or {}).get(
                    "terminal")
                if (terminal_component is not None and not is_timeout
                        and result in {"win", "loss"}):
                    expected_sign = 1.0 if result == "win" else -1.0
                    if (float(terminal_component) == 0.0
                            or np.sign(float(terminal_component)) != expected_sign):
                        self._terminal_reward_mismatches += 1
                if transition_rewards is not None and index < len(
                        np.asarray(transition_rewards).reshape(-1)):
                    self.logger.record_mean(
                        "outcome/terminal_transition_reward",
                        float(np.asarray(transition_rewards).reshape(-1)[index]))
        denominator = max(1, self._transition_total)
        self.logger.record(
            "terminal/any_count", self._terminal_total)
        self.logger.record(
            "terminal/any_rate", self._terminal_total / denominator)
        for reason, count in sorted(self._terminal_counts.items()):
            self.logger.record(f"terminal/{reason}_count", count)
            self.logger.record(f"terminal/{reason}_rate", count / denominator)
            self.logger.record(
                f"terminal_episode/{reason}_rate",
                count / max(1, self._terminal_total))
        for result, count in sorted(self._outcome_counts.items()):
            self.logger.record(f"outcome/{result}_count", count)
            self.logger.record(
                f"outcome/{result}_rate",
                count / max(1, self._terminal_total))
        self.logger.record(
            "reward_diagnostic/terminal_result_sign_mismatch_count",
            self._terminal_reward_mismatches)
        for namespace, buckets in (
                ("opponent_profile", self._profile_outcomes),
                ("curriculum_stage", self._stage_outcomes)):
            for label, bucket in sorted(buckets.items()):
                episodes = max(1, bucket["episodes"])
                self.logger.record(
                    f"{namespace}/{label}/episodes", bucket["episodes"])
                for metric in ("decisive_win", "decisive_loss", "timeout"):
                    self.logger.record(
                        f"{namespace}/{label}/{metric}_rate",
                        bucket[metric] / episodes)
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
        absolute_maxima = {}
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
            absolute_max = float(np.max(np.abs(finite)))
            absolute_maxima[name] = absolute_max
            self.logger.record(f"critic/{name}_abs_max", absolute_max)
        reward_scale = max(absolute_maxima.get("reward", 0.0), 1e-6)
        value_scale_ratio = absolute_maxima.get("value", 0.0) / reward_scale
        self.logger.record(
            "critic/value_to_reward_abs_max_ratio", value_scale_ratio)
        explained = None
        valid = np.isfinite(values) & np.isfinite(returns)
        if np.any(valid):
            target_variance = float(np.var(returns[valid]))
            if target_variance > 0.0:
                explained = 1.0 - float(np.var(
                    returns[valid] - values[valid])) / target_variance
                self.logger.record(
                    "critic/rollout_explained_variance", explained)
        unhealthy = value_scale_ratio > 3.0 and (
            explained is None or explained < -1.0)
        self._unhealthy_rollouts = (
            self._unhealthy_rollouts + 1 if unhealthy else 0)
        self.logger.record(
            "critic/consecutive_unhealthy_rollouts",
            self._unhealthy_rollouts)
        if unhealthy and self._unhealthy_rollouts in {1, 5, 20}:
            logging.warning(
                "Critic scale is unstable for %s consecutive rollout(s): "
                "value/reward abs-max ratio=%.2f, explained_variance=%s",
                self._unhealthy_rollouts, value_scale_ratio,
                "unavailable" if explained is None else f"{explained:.3f}")


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


class CurriculumProgressCallback(BaseCallback):
    """Coordinate fixed or mastery-gated deterministic matchup schedulers."""

    MODEL_STATE_ATTRIBUTE = "playersim_curriculum_progress"

    def __init__(self, curriculum):
        super().__init__(verbose=0)
        self.curriculum = curriculum
        self.progression = str(
            curriculum.get("progression") or "fixed_timesteps")
        self._active_stage_index = None
        self._stage_selected_timestep = 0
        self._stage_entry_timestep = 0
        self._recent_outcomes = deque()
        # Handicap evidence has its own profile-filtered window.  Sharing the
        # aggregate mastery deque can make a ratchet impossible when a stage's
        # profile bag also contains unhandicapped opponents (for example,
        # combat-v5 full_pool is eight scripted games and two novice games).
        self._handicap_outcomes = deque()
        self._transition_history = []
        self._pending_activation_workers = set()
        self._stale_stage_episodes = 0
        # Annealed opponent handicap: None means "initialize from the active
        # stage's configured start on the next broadcast".
        self._handicap_epsilon = None
        self._handicap_enabled = any(
            stage.get("handicap")
            for stage in (curriculum.get("stages") or ()))

    def _stage_index(self):
        selected = 0
        for index, stage in enumerate(self.curriculum["stages"]):
            if self.num_timesteps < int(stage["start_timestep"]):
                break
            selected = index
        return selected

    def _window_size(self, stage_index=None):
        selected = self._active_stage_index if stage_index is None \
            else int(stage_index)
        if selected is None:
            selected = 0
        stage = self.curriculum["stages"][selected]
        gate = stage.get("advance_when") or {}
        # A final stage has no mastery gate.  Retain an aggregate diagnostic
        # window at least as large as its separate handicap-evidence window.
        handicap = stage.get("handicap") or {}
        return max(1, int(gate.get("window_episodes", 1)),
                   int(handicap.get("window_episodes", 0)))

    def _handicap_window_size(self, stage_index=None):
        selected = self._active_stage_index if stage_index is None \
            else int(stage_index)
        if selected is None:
            selected = 0
        handicap = self.curriculum["stages"][selected].get("handicap") or {}
        return max(1, int(handicap.get("window_episodes", 1)))

    def _persist_mastery_state(self):
        if self.progression != "mastery":
            return
        setattr(self.model, self.MODEL_STATE_ATTRIBUTE, {
            "curriculum_id": self.curriculum.get("id"),
            "curriculum_version": self.curriculum.get("version"),
            "stage_index": int(self._active_stage_index or 0),
            "stage_selected_timestep": int(self._stage_selected_timestep),
            "stage_entry_timestep": (
                None if self._stage_entry_timestep is None
                else int(self._stage_entry_timestep)),
            "recent_outcomes": list(self._recent_outcomes),
            "handicap_outcomes": list(self._handicap_outcomes),
            "transition_history": list(self._transition_history),
            "pending_activation_workers": sorted(
                self._pending_activation_workers),
            "stale_stage_episodes": int(self._stale_stage_episodes),
            "handicap_epsilon": (
                None if self._handicap_epsilon is None
                else float(self._handicap_epsilon)),
        })

    @staticmethod
    def _normalize_outcome_record(record):
        if isinstance(record, dict):
            return {
                "outcome": str(record.get("outcome") or "unknown"),
                "opponent_profile": str(
                    record.get("opponent_profile") or "unknown"),
                "opponent_handicap": float(
                    record.get("opponent_handicap") or 0.0),
            }
        # Older callback state stored only the aggregate outcome.  It remains
        # useful for aggregate gates but cannot satisfy a profile requirement.
        return {"outcome": str(record), "opponent_profile": "unknown",
                "opponent_handicap": 0.0}

    def _restore_mastery_state(self):
        state = getattr(self.model, self.MODEL_STATE_ATTRIBUTE, None)
        if (not isinstance(state, dict)
                or state.get("curriculum_id") != self.curriculum.get("id")
                or state.get("curriculum_version")
                != self.curriculum.get("version")):
            self._active_stage_index = 0
            self._stage_selected_timestep = int(self.num_timesteps)
            self._stage_entry_timestep = int(self.num_timesteps)
            self._recent_outcomes = deque(maxlen=self._window_size(0))
            self._handicap_outcomes = deque(
                maxlen=self._handicap_window_size(0))
            self._transition_history = []
            self._pending_activation_workers = set()
            self._stale_stage_episodes = 0
            self._handicap_epsilon = None
            self._persist_mastery_state()
            return
        maximum = len(self.curriculum["stages"]) - 1
        self._active_stage_index = min(
            maximum, max(0, int(state.get("stage_index", 0))))
        restored_entry = state.get(
            "stage_entry_timestep", self.num_timesteps)
        self._stage_entry_timestep = (
            None if restored_entry is None else int(restored_entry))
        self._stage_selected_timestep = int(state.get(
            "stage_selected_timestep",
            self._stage_entry_timestep
            if self._stage_entry_timestep is not None
            else self.num_timesteps))
        self._recent_outcomes = deque(
            [self._normalize_outcome_record(record) for record in
             list(state.get("recent_outcomes") or ())],
            maxlen=self._window_size(self._active_stage_index))
        restored_handicap = state.get("handicap_epsilon")
        self._handicap_epsilon = (
            None if restored_handicap is None else float(restored_handicap))
        if "handicap_outcomes" in state:
            handicap_records = list(state.get("handicap_outcomes") or ())
        else:
            # Backward-compatible migration: older checkpoints only persisted
            # the aggregate window, so retain any qualifying evidence still
            # present there.
            handicap_records = list(self._recent_outcomes)
        normalized_handicap_records = [
            self._normalize_outcome_record(record)
            for record in handicap_records]
        self._handicap_outcomes = deque(
            [record for record in normalized_handicap_records
             if self._is_current_handicap_outcome(record)],
            maxlen=self._handicap_window_size(self._active_stage_index))
        self._transition_history = list(
            state.get("transition_history") or ())
        self._pending_activation_workers = {
            max(0, int(index)) for index in
            state.get("pending_activation_workers") or ()}
        self._stale_stage_episodes = max(
            0, int(state.get("stale_stage_episodes", 0)))

    def _training_environment_count(self):
        count = getattr(self.training_env, "num_envs", None)
        if count is None:
            dones = self.locals.get("dones")
            count = len(dones) if dones is not None else 1
        return max(1, int(count))

    def _outcome_rates(self, opponent_profile=None, full_strength_only=False):
        records = list(self._recent_outcomes)
        if opponent_profile is not None:
            records = [
                record for record in records
                if record["opponent_profile"] == opponent_profile]
        if full_strength_only:
            # Wins earned against a handicapped opponent are ramp progress,
            # not mastery evidence.
            records = [
                record for record in records
                if not float(record.get("opponent_handicap", 0.0))]
        episodes = len(records)
        denominator = max(1, episodes)
        return {
            "episodes": episodes,
            "decisive_win_rate": sum(
                record["outcome"] == "win" for record in records)
                / denominator,
            "decisive_loss_rate": sum(
                record["outcome"] == "loss" for record in records)
                / denominator,
            "timeout_rate": sum(
                record["outcome"] == "timeout" for record in records)
                / denominator,
        }

    def _record_mastery_outcomes(self):
        if self._active_stage_index is None:
            return
        active_name = self.curriculum["stages"][
            self._active_stage_index]["name"]
        infos = list(self.locals.get("infos", ()) or ())
        dones = self.locals.get("dones")
        changed = False
        for index, info in enumerate(infos):
            if (dones is not None and index < len(dones)
                    and not bool(dones[index])):
                continue
            if info.get("curriculum_stage") != active_name:
                if index in self._pending_activation_workers:
                    self._stale_stage_episodes += 1
                    changed = True
                continue
            if index in self._pending_activation_workers:
                self._pending_activation_workers.discard(index)
                changed = True
            reason = str(info.get("terminal_reason") or "")
            if evaluation_is_timeout(reason):
                outcome = "timeout"
            else:
                outcome = canonical_evaluation_game_result(
                    info.get("game_result"))
            record = {
                "outcome": outcome,
                "opponent_profile": str(
                    info.get("opponent_profile") or "unknown"),
                "opponent_handicap": float(
                    info.get("opponent_handicap") or 0.0),
            }
            self._recent_outcomes.append(record)
            if self._is_current_handicap_outcome(record):
                self._handicap_outcomes.append(record)
            changed = True
        if (self._stage_entry_timestep is None
                and not self._pending_activation_workers):
            self._stage_entry_timestep = int(self.num_timesteps)
            if self._transition_history:
                self._transition_history[-1]["activation_timestep"] = int(
                    self.num_timesteps)
            logging.info(
                "All training workers activated curriculum stage %s at "
                "timestep %s after %s stale-stage episodes",
                active_name, self.num_timesteps, self._stale_stage_episodes)
            changed = True
        if changed:
            self._persist_mastery_state()

    def _stage_handicap(self, stage_index=None):
        selected = self._active_stage_index if stage_index is None \
            else int(stage_index)
        if selected is None:
            return None
        return self.curriculum["stages"][selected].get("handicap")

    def _is_current_handicap_outcome(self, record):
        config = self._stage_handicap()
        if config is None or self._handicap_epsilon is None:
            return False
        current = float(self._handicap_epsilon)
        return (
            current > 0.0
            and record["opponent_profile"] in set(config["profiles"])
            and float(record.get("opponent_handicap", 0.0)) == current
        )

    def _handicap_rates(self):
        """Rolling outcomes against the handicapped profiles at the live
        epsilon; records from earlier ratchet levels no longer count."""
        config = self._stage_handicap()
        if config is None:
            return None
        current = float(self._handicap_epsilon or 0.0)
        profiles = set(config["profiles"])
        records = [
            record for record in self._handicap_outcomes
            if record["opponent_profile"] in profiles
            and float(record.get("opponent_handicap", 0.0)) == current]
        window = int(config["window_episodes"])
        records = records[-window:]
        episodes = len(records)
        return {
            "episodes": episodes,
            "decisive_win_rate": sum(
                record["outcome"] == "win" for record in records)
                / max(1, episodes),
            "window": window,
            "target": float(config["min_decisive_win_rate"]),
            "step": float(config["step"]),
            "profiles": sorted(profiles),
        }

    def _broadcast_handicap(self):
        """Send the live (epsilon, profiles) pair to every training worker."""
        if self.progression != "mastery" or not self._handicap_enabled:
            return
        config = self._stage_handicap()
        if config is None:
            self._handicap_epsilon = 0.0
        elif self._handicap_epsilon is None:
            self._handicap_epsilon = float(config["start"])
        self.training_env.env_method(
            "set_opponent_handicap", float(self._handicap_epsilon),
            list(config["profiles"]) if config else [])

    def _maybe_ratchet_handicap(self):
        if (self.progression != "mastery" or not self._handicap_enabled
                or not float(self._handicap_epsilon or 0.0)):
            return
        rates = self._handicap_rates()
        if (rates is None or rates["episodes"] < rates["window"]
                or rates["decisive_win_rate"] < rates["target"]):
            return
        previous = float(self._handicap_epsilon)
        self._handicap_epsilon = max(0.0, round(previous - rates["step"], 6))
        # Evidence is specific to one epsilon; the next step must earn a fresh
        # qualifying window rather than wait for stale records to age out.
        self._handicap_outcomes.clear()
        self.training_env.env_method(
            "set_opponent_handicap", float(self._handicap_epsilon),
            rates["profiles"])
        self._persist_mastery_state()
        logging.info(
            "Opponent handicap ratcheted from %.2f to %.2f for profiles %s "
            "at timestep %s (win rate %.2f over %s episodes)",
            previous, self._handicap_epsilon, rates["profiles"],
            self.num_timesteps, rates["decisive_win_rate"],
            rates["episodes"])

    def _mastery_ready(self):
        if self._active_stage_index >= len(self.curriculum["stages"]) - 1:
            return False
        stage = self.curriculum["stages"][self._active_stage_index]
        next_stage = self.curriculum["stages"][self._active_stage_index + 1]
        gate = stage.get("advance_when") or {}
        rates = self._outcome_rates()
        aggregate_ready = (
            rates["episodes"] >= int(gate["window_episodes"])
            and self.num_timesteps >= int(next_stage["start_timestep"])
            and self._stage_entry_timestep is not None
            and self.num_timesteps - self._stage_entry_timestep
            >= int(gate["min_stage_timesteps"])
            and rates["decisive_win_rate"]
            >= float(gate["min_decisive_win_rate"])
            and rates["decisive_loss_rate"]
            <= float(gate["max_decisive_loss_rate"])
            and rates["timeout_rate"] <= float(gate["max_timeout_rate"])
        )
        if not aggregate_ready:
            return False
        # A handicapped stage cannot be mastered until its anneal completes;
        # profile floors then only count full-strength episodes.
        if (self._stage_handicap() is not None
                and float(self._handicap_epsilon or 0.0) > 0.0):
            return False
        for profile, requirement in (
                gate.get("profile_requirements") or {}).items():
            profile_rates = self._outcome_rates(
                profile, full_strength_only=True)
            if (profile_rates["episodes"]
                    < int(requirement["min_episodes"])
                    or profile_rates["decisive_win_rate"]
                    < float(requirement["min_decisive_win_rate"])):
                return False
        return True

    def _deadline_ready(self):
        if self._active_stage_index >= len(self.curriculum["stages"]) - 1:
            return False
        gate = self.curriculum["stages"][
            self._active_stage_index].get("advance_when") or {}
        maximum_steps = gate.get("max_stage_timesteps")
        return (
            maximum_steps is not None
            and self.num_timesteps - self._stage_selected_timestep
            >= int(maximum_steps)
        )

    def _advance_reason(self):
        if self._mastery_ready():
            return "mastery"
        if self._deadline_ready():
            return "deadline"
        return None

    def _record_mastery_metrics(self):
        rates = self._outcome_rates()
        self.logger.record(
            "curriculum/mastery_window_episodes", rates["episodes"])
        self.logger.record(
            "curriculum/mastery_decisive_win_rate",
            rates["decisive_win_rate"])
        self.logger.record(
            "curriculum/mastery_decisive_loss_rate",
            rates["decisive_loss_rate"])
        self.logger.record(
            "curriculum/mastery_timeout_rate", rates["timeout_rate"])
        if self._active_stage_index is not None:
            gate = self.curriculum["stages"][
                self._active_stage_index].get("advance_when") or {}
            for profile in sorted(
                    (gate.get("profile_requirements") or {}).keys()):
                profile_rates = self._outcome_rates(
                    profile, full_strength_only=True)
                requirement = gate["profile_requirements"][profile]
                prefix = f"curriculum/mastery_{profile}"
                self.logger.record(
                    f"{prefix}_episodes", profile_rates["episodes"])
                self.logger.record(
                    f"{prefix}_decisive_win_rate",
                    profile_rates["decisive_win_rate"])
                self.logger.record(
                    f"{prefix}_ready",
                    float(
                        profile_rates["episodes"]
                        >= int(requirement["min_episodes"])
                        and profile_rates["decisive_win_rate"]
                        >= float(requirement[
                            "min_decisive_win_rate"])))
        self.logger.record(
            "curriculum/stage_elapsed_timesteps",
            0 if self._stage_entry_timestep is None else
            max(0, self.num_timesteps - self._stage_entry_timestep))
        self.logger.record(
            "curriculum/stage_selected_elapsed_timesteps",
            max(0, self.num_timesteps - self._stage_selected_timestep))
        self.logger.record(
            "curriculum/stage_activation_pending_workers",
            len(self._pending_activation_workers))
        self.logger.record(
            "curriculum/stale_stage_episodes",
            self._stale_stage_episodes)
        maximum_steps = gate.get("max_stage_timesteps") \
            if self._active_stage_index is not None else None
        if maximum_steps is not None:
            self.logger.record(
                "curriculum/deadline_remaining_timesteps",
                max(0, int(maximum_steps) - (
                    self.num_timesteps - self._stage_selected_timestep)))
        self.logger.record(
            "curriculum/mastery_ready", float(self._mastery_ready()))
        self.logger.record(
            "curriculum/deadline_ready", float(self._deadline_ready()))
        if self._handicap_enabled:
            self.logger.record(
                "curriculum/handicap_epsilon",
                float(self._handicap_epsilon or 0.0))
            handicap_rates = self._handicap_rates()
            if handicap_rates is not None:
                self.logger.record(
                    "curriculum/handicap_window_episodes",
                    handicap_rates["episodes"])
                self.logger.record(
                    "curriculum/handicap_decisive_win_rate",
                    handicap_rates["decisive_win_rate"])

    def _broadcast(self, force=False, stage_index=None):
        if stage_index is None:
            stage_index = (
                self._stage_index() if self.progression == "fixed_timesteps"
                else int(self._active_stage_index or 0))
        if not force and stage_index == self._active_stage_index:
            return
        if self.progression == "mastery":
            self.training_env.env_method(
                "set_curriculum_stage", stage_index,
                int(self.num_timesteps))
        else:
            self.training_env.env_method(
                "set_curriculum_timestep", int(self.num_timesteps))
        self._active_stage_index = stage_index
        self._broadcast_handicap()
        stage = self.curriculum["stages"][stage_index]
        self.logger.record("curriculum/stage_index", stage_index)
        logging.info(
            "Opponent curriculum entered stage %s at timestep %s",
            stage["name"], self.num_timesteps)

    def _on_training_start(self):
        if self.progression == "mastery":
            self._restore_mastery_state()
            self._broadcast(force=True, stage_index=self._active_stage_index)
            self._record_mastery_metrics()
        else:
            self._broadcast(force=True)

    def _on_step(self):
        if self.progression != "mastery":
            self._broadcast()
            return True
        self._record_mastery_outcomes()
        self._maybe_ratchet_handicap()
        self._record_mastery_metrics()
        advance_reason = self._advance_reason()
        if advance_reason is not None:
            previous = self.curriculum["stages"][
                self._active_stage_index]["name"]
            next_stage_index = self._active_stage_index + 1
            next_stage = self.curriculum["stages"][next_stage_index]["name"]
            self._transition_history.append({
                "from_stage": previous,
                "to_stage": next_stage,
                "timestep": int(self.num_timesteps),
                "activation_timestep": None,
                "reason": advance_reason,
            })
            self._active_stage_index += 1
            self._stage_selected_timestep = int(self.num_timesteps)
            self._stage_entry_timestep = None
            self._pending_activation_workers = set(range(
                self._training_environment_count()))
            self._stale_stage_episodes = 0
            self._recent_outcomes = deque(
                maxlen=self._window_size(self._active_stage_index))
            self._handicap_outcomes = deque(
                maxlen=self._handicap_window_size(
                    self._active_stage_index))
            # Each stage anneals from its own configured start.
            self._handicap_epsilon = None
            self._persist_mastery_state()
            transition_logger = (
                logging.warning if advance_reason == "deadline"
                else logging.info)
            transition_logger(
                "Opponent curriculum advanced from %s to %s via %s at "
                "timestep %s",
                previous, next_stage, advance_reason, self.num_timesteps)
            self.logger.record(
                "curriculum/advance_via_mastery",
                float(advance_reason == "mastery"))
            self.logger.record(
                "curriculum/advance_via_deadline",
                float(advance_reason == "deadline"))
            self._broadcast(force=True, stage_index=self._active_stage_index)
            self._persist_mastery_state()
        return True


class TrainingProvenanceCallback(BaseCallback):
    """Stamp each rollout's game records with an attributable timestep."""

    def _on_step(self):
        return True

    def _on_rollout_start(self):
        self.training_env.env_method(
            "set_training_timestep", int(self.num_timesteps))


def curriculum_progress_manifest(model, curriculum):
    """Return auditable runtime curriculum progress for a run manifest."""
    if curriculum is None:
        return None
    result = {
        "curriculum_id": curriculum.get("id"),
        "curriculum_version": curriculum.get("version"),
        "progression": curriculum.get("progression"),
        "state": None,
    }
    state = getattr(
        model, CurriculumProgressCallback.MODEL_STATE_ATTRIBUTE, None) \
        if model is not None else None
    if isinstance(state, dict):
        normalized = json_safe(state)
        stages = curriculum.get("stages") or ()
        stage_index = int(normalized.get("stage_index", 0))
        if 0 <= stage_index < len(stages):
            normalized["stage_name"] = stages[stage_index].get("name")
        result["state"] = normalized
    return result


def canonical_evaluation_game_result(value):
    """Normalize terminal result labels to the learned agent's perspective."""
    raw = str(value or "").strip().casefold().replace("-", "_")
    if raw.startswith("win"):
        return "win"
    if raw.startswith("loss"):
        return "loss"
    if raw.startswith("draw") or raw in {"tie", "tied"}:
        return "draw"
    raise RuntimeError(
        f"Evaluation episode ended without a canonical game_result: {value!r}")


def canonical_evaluation_terminal_reason(value):
    reason = str(value or "").strip().casefold()
    reason = re.sub(r"[^a-z0-9]+", "_", reason).strip("_")
    if not reason:
        raise RuntimeError(
            "Evaluation episode ended without a terminal_reason")
    return reason


def evaluation_is_timeout(terminal_reason):
    reason = canonical_evaluation_terminal_reason(terminal_reason)
    return reason in {"turn_limit", "time_limit", "timeout"} or (
        "turn_limit" in reason or reason.endswith("_timeout"))


def _student_t_critical_95(degrees_of_freedom):
    """Return the two-sided 95% Student-t critical value without SciPy."""
    df = int(degrees_of_freedom)
    if df <= 0:
        raise ValueError("degrees_of_freedom must be positive")
    # Exact table values where the small-sample correction matters most.
    table = (
        12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306,
        2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131, 2.120,
        2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064,
        2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
    )
    if df <= len(table):
        return table[df - 1]
    # Cornish-Fisher expansion is accurate to better than the precision used
    # in reports for the production 32-pair suite and converges to Normal.
    z = 1.959963984540054
    inverse_df = 1.0 / df
    return (
        z
        + (z**3 + z) * inverse_df / 4.0
        + (5.0 * z**5 + 16.0 * z**3 + 3.0 * z)
        * inverse_df**2 / 96.0
        + (3.0 * z**7 + 19.0 * z**5 + 17.0 * z**3 - 15.0 * z)
        * inverse_df**3 / 384.0
    )


def _qualification_points(episode):
    if episode.get("timeout"):
        return 0.0
    result = episode.get("game_result")
    if result == "win":
        return 1.0
    if result == "draw":
        return 0.5
    return 0.0


def _paired_qualification_units(episodes):
    """Return adjacent paired-seat unit scores, or None if not fully paired."""
    if len(episodes) % 2:
        return None
    units = []
    for index in range(0, len(episodes), 2):
        first = episodes[index]
        second = episodes[index + 1]
        first_case = first.get("case")
        second_case = second.get("case")
        if not isinstance(first_case, dict) or not isinstance(
                second_case, dict):
            return None
        is_pair = (
            first_case.get("seed") is not None
            and first_case.get("seed") == second_case.get("seed")
            and first_case.get("agent_is_p1") is True
            and second_case.get("agent_is_p1") is False
            and bool(first_case.get("p1_deck"))
            and bool(first_case.get("p2_deck"))
            and first_case.get("p1_deck") != first_case.get("p2_deck")
            and first_case.get("p1_deck") == second_case.get("p2_deck")
            and first_case.get("p2_deck") == second_case.get("p1_deck")
            and bool(first_case.get("opponent_profile"))
            and first_case.get("opponent_profile")
            == second_case.get("opponent_profile")
        )
        if not is_pair:
            return None
        units.append(
            (_qualification_points(first) + _qualification_points(second))
            / 2.0)
    return units


def qualification_score_interval(episodes):
    """Conservative 95% interval that respects paired-seat case clustering.

    The Wilson component keeps all-win/all-loss suites from reporting a zero
    uncertainty interval and supports the half point awarded to a real draw.
    When every adjacent case is a seat-swapped pair, a Student-t interval over
    pair means is added and the conservative envelope is returned.
    """
    count = len(episodes)
    if count <= 0:
        raise ValueError("qualification interval requires at least one episode")
    total_points = sum(_qualification_points(item) for item in episodes)
    score = total_points / count
    z = 1.959963984540054
    z_squared = z * z
    denominator = 1.0 + z_squared / count
    wilson_center = (score + z_squared / (2.0 * count)) / denominator
    wilson_half_width = (
        z * math.sqrt(
            score * (1.0 - score) / count
            + z_squared / (4.0 * count * count))
        / denominator
    )
    wilson_lower = max(0.0, wilson_center - wilson_half_width)
    wilson_upper = min(1.0, wilson_center + wilson_half_width)
    interval = {
        "method": "wilson-score",
        "confidence": 0.95,
        "lower_bound": float(wilson_lower),
        "upper_bound": float(wilson_upper),
        "episode_units": int(count),
        "paired_units": 0,
        "wilson_lower_bound": float(wilson_lower),
        "wilson_upper_bound": float(wilson_upper),
    }

    paired_units = _paired_qualification_units(episodes)
    if paired_units is None:
        return interval
    interval["paired_units"] = int(len(paired_units))
    if len(paired_units) < 2:
        return interval
    pair_count = len(paired_units)
    pair_mean = float(np.mean(paired_units))
    pair_std = float(np.std(paired_units, ddof=1))
    pair_half_width = (
        _student_t_critical_95(pair_count - 1)
        * pair_std / math.sqrt(pair_count)
    )
    paired_lower = max(0.0, pair_mean - pair_half_width)
    paired_upper = min(1.0, pair_mean + pair_half_width)
    interval.update({
        "method": "wilson-score+paired-t-envelope",
        "lower_bound": float(min(wilson_lower, paired_lower)),
        "upper_bound": float(max(wilson_upper, paired_upper)),
        "paired_units": int(pair_count),
        "paired_lower_bound": float(paired_lower),
        "paired_upper_bound": float(paired_upper),
    })
    return interval


def summarize_evaluation_episodes(episodes):
    """Validate per-case outcomes and derive the checkpoint promotion score."""
    if not episodes:
        raise RuntimeError("Evaluation produced no completed episodes")
    normalized = []
    for index, episode in enumerate(episodes):
        item = dict(episode)
        raw_result = item.get("raw_game_result", item.get("game_result"))
        result = canonical_evaluation_game_result(raw_result)
        terminal_reason = canonical_evaluation_terminal_reason(
            item.get("terminal_reason"))
        reward = float(item.get("reward", 0.0))
        length = int(item.get("length", 0))
        if not np.isfinite(reward):
            raise RuntimeError(
                f"Evaluation episode {index} produced non-finite reward")
        if length <= 0:
            raise RuntimeError(
                f"Evaluation episode {index} produced invalid length {length}")
        timed_out = evaluation_is_timeout(terminal_reason)
        item.update({
            "case_index": int(item.get("case_index", index)),
            "raw_game_result": str(raw_result),
            "game_result": result,
            "terminal_reason": terminal_reason,
            "reward": reward,
            "length": length,
            "timeout": timed_out,
            "decisive": bool(not timed_out and result in {"win", "loss"}),
        })
        normalized.append(item)

    decisive_wins = sum(
        item["decisive"] and item["game_result"] == "win"
        for item in normalized)
    decisive_losses = sum(
        item["decisive"] and item["game_result"] == "loss"
        for item in normalized)
    non_timeout_draws = sum(
        not item["timeout"] and item["game_result"] == "draw"
        for item in normalized)
    timeouts = sum(item["timeout"] for item in normalized)
    rewards = np.asarray(
        [item["reward"] for item in normalized], dtype=np.float64)
    lengths = np.asarray(
        [item["length"] for item in normalized], dtype=np.float64)
    decisive_score = int(decisive_wins - decisive_losses)
    summary = {
        "episodes": len(normalized),
        "decisive_wins": int(decisive_wins),
        "decisive_losses": int(decisive_losses),
        "non_timeout_draws": int(non_timeout_draws),
        "timeouts": int(timeouts),
        "decisive_score": decisive_score,
        "decisive_win_rate": float(decisive_wins / len(normalized)),
        "timeout_rate": float(timeouts / len(normalized)),
        "mean_reward": float(np.mean(rewards)),
        "std_reward": float(np.std(rewards)),
        "mean_ep_length": float(np.mean(lengths)),
        "qualification_score": float(
            (decisive_wins + 0.5 * non_timeout_draws) / len(normalized)),
    }
    summary["qualification_interval"] = qualification_score_interval(
        normalized)
    # Fixed cases make counts comparable across checkpoints.  Shaped reward is
    # deliberately last: a timeout-heavy policy can never become "best" merely
    # because it accumulated a favorable life-lead shaping return.
    promotion_key = (
        summary["decisive_wins"],
        summary["decisive_score"],
        -summary["timeouts"],
        summary["mean_reward"],
    )
    return normalized, summary, promotion_key


def install_fixed_evaluation_schedule(eval_env, schedule):
    """Rewind and install the exact same public cases for one checkpoint."""
    if int(getattr(eval_env, "num_envs", 1)) != 1:
        raise RuntimeError(
            "Fixed periodic evaluation currently requires exactly one VecEnv")
    cases = [dict(case) for case in schedule]
    try:
        eval_env.env_method("reset_episode_schedule")
        eval_env.env_method("set_episode_schedule", cases)
    except Exception as error:
        raise RuntimeError(
            "Evaluation environments must implement reset_episode_schedule() "
            "and set_episode_schedule(cases)") from error


def _capture_evaluation_terminal_info(info):
    """Extract the process-safe terminal fields retained by evaluation."""
    info = info or {}
    captured = {
        "raw_game_result": info.get("game_result"),
        "terminal_reason": info.get("terminal_reason"),
        "resolved_case": {
            "seed": info.get("episode_seed"),
            "p1_deck": info.get("p1_deck"),
            "p2_deck": info.get("p2_deck"),
            "agent_is_p1": info.get("agent_is_p1"),
            "opponent_profile": info.get("opponent_profile"),
        },
    }
    debug = info.get("evaluation_debug")
    if debug is not None:
        if not isinstance(debug, dict):
            raise RuntimeError(
                "Evaluation terminal debug payload must be an object")
        captured["debug"] = json_safe(debug)
    return captured


def _build_evaluation_episode(case_index, case, outcome, reward, length):
    """Join one fixed case, terminal record, and evaluate_policy result."""
    episode = {
        "case_index": int(case_index),
        "case": dict(case),
        "resolved_case": dict(outcome["resolved_case"]),
        "raw_game_result": outcome["raw_game_result"],
        "terminal_reason": outcome["terminal_reason"],
        "reward": float(reward),
        "length": int(length),
    }
    # Optional and additive: histories produced before evaluation replay
    # capture, plus synthetic callback tests, remain valid without this field.
    if outcome.get("debug") is not None:
        episode["debug"] = deepcopy(outcome["debug"])
    return episode


def _evaluation_debug_summary(debug):
    """Build the compact index stored beside a heavy evaluation sidecar."""
    if not isinstance(debug, dict):
        return None
    trace = debug.get("trace")
    trace = trace if isinstance(trace, (list, tuple)) else ()
    replay = debug.get("replay")
    replay_actions = replay.get("actions") \
        if isinstance(replay, dict) else None
    replay_actions = replay_actions \
        if isinstance(replay_actions, (list, tuple)) else ()

    actors = {}
    for event in trace:
        if not isinstance(event, dict):
            continue
        actor = str(event.get("actor") or "unknown")
        actors[actor] = actors.get(actor, 0) + 1

    capture = debug.get("capture")
    compact_capture = {}
    degraded = False
    if isinstance(capture, dict):
        for scope in ("trace", "replay", "terminal"):
            raw = capture.get(scope)
            if not isinstance(raw, dict):
                continue
            selected = {}
            for key in (
                    "recorded_events", "dropped_events", "serialized_bytes",
                    "sanitization_omissions", "serialization_errors"):
                if key in raw:
                    selected[key] = raw.get(key)
            compact_capture[scope] = selected
            degraded = degraded or any(
                int(selected.get(key, 0) or 0) > 0 for key in (
                    "dropped_events", "sanitization_omissions",
                    "serialization_errors")
            )
        capture_errors = capture.get("errors")
        capture_error_count = len(capture_errors) \
            if isinstance(capture_errors, (list, tuple)) else 0
        compact_capture["error_count"] = capture_error_count
        degraded = degraded or capture_error_count > 0
    else:
        capture_error_count = 0

    catalog = debug.get("card_catalog")
    if isinstance(catalog, dict):
        catalog_entries = catalog.get("entries")
        catalog_count = len(catalog_entries) \
            if isinstance(catalog_entries, (list, tuple)) \
            else int(catalog.get("recorded_entries", 0) or 0)
        catalog_omitted = int(catalog.get("omitted_entries", 0) or 0)
    elif isinstance(catalog, (list, tuple)):
        catalog_count, catalog_omitted = len(catalog), 0
    else:
        catalog_count, catalog_omitted = 0, 0
    degraded = degraded or catalog_omitted > 0

    terminal = debug.get("terminal")
    terminal = terminal if isinstance(terminal, dict) else {}
    evaluator = debug.get("evaluator")
    evaluator_summary = evaluator.get("summary") \
        if isinstance(evaluator, dict) else None
    return json_safe({
        "schema_version": 1,
        "trace_event_count": len(trace),
        "trace_actor_counts": actors,
        "replay_action_count": len(replay_actions),
        "card_catalog_count": catalog_count,
        "card_catalog_omitted": catalog_omitted,
        "capture_status": "degraded" if degraded else (
            "complete" if isinstance(capture, dict) else "not_recorded"),
        "capture": compact_capture,
        "terminal": {
            key: terminal.get(key) for key in (
                "game_result", "terminal_reason", "reward", "done",
                "truncated") if key in terminal
        },
        "evaluator": evaluator_summary
        if isinstance(evaluator_summary, dict) else None,
    })


def _persist_evaluation_debug_sidecars(
        episodes, *, timestep, evaluation_history_path):
    """Externalize inline per-game debug data before history publication.

    Worker results intentionally retain inline ``debug`` for process-boundary
    compatibility.  Only the callback that owns ``evaluations.json`` knows its
    final directory, so it atomically writes the heavy payloads and substitutes
    small, relative references there.
    """
    normalized = [dict(episode) for episode in episodes]
    if not any(episode.get("debug") is not None for episode in normalized):
        return normalized
    if not evaluation_history_path:
        raise RuntimeError(
            "Evaluation debug sidecars require an evaluation history path")
    try:
        safe_timestep = int(timestep)
    except (TypeError, ValueError, OverflowError) as error:
        raise RuntimeError(
            f"Invalid evaluation timestep for debug sidecars: {timestep!r}") \
            from error
    if safe_timestep < 0:
        raise RuntimeError(
            "Evaluation timestep for debug sidecars cannot be negative")

    history_directory = os.path.dirname(os.path.abspath(
        os.fspath(evaluation_history_path)))
    seen_case_indices = set()
    externalized = []
    for ordinal, episode in enumerate(normalized):
        item = dict(episode)
        debug = item.pop("debug", None)
        if debug is None:
            externalized.append(item)
            continue
        if not isinstance(debug, dict):
            raise RuntimeError(
                f"Evaluation case {ordinal} debug payload must be an object")
        try:
            case_index = int(item.get("case_index", ordinal))
        except (TypeError, ValueError, OverflowError) as error:
            raise RuntimeError(
                f"Invalid evaluation case index for debug sidecar: "
                f"{item.get('case_index')!r}") from error
        if case_index < 0 or case_index in seen_case_indices:
            raise RuntimeError(
                "Evaluation debug sidecars require unique non-negative case "
                f"indices; received {case_index}")
        seen_case_indices.add(case_index)

        relative_path = os.path.join(
            "games", str(safe_timestep),
            f"case_{case_index:03d}.json.gz")
        sidecar_path = os.path.abspath(os.path.join(
            history_directory, relative_path))
        if os.path.commonpath((history_directory, sidecar_path)) \
                != history_directory:
            raise RuntimeError(
                "Evaluation debug sidecar resolved outside history directory")
        write_gzip_json_atomic(sidecar_path, debug)

        trace = debug.get("trace")
        replay = debug.get("replay")
        replay_actions = replay.get("actions") \
            if isinstance(replay, dict) else None
        item.update({
            "debug_path": os.path.relpath(
                sidecar_path, history_directory).replace(os.sep, "/"),
            "debug_sha256": sha256_file(sidecar_path),
            "debug_size_bytes": os.path.getsize(sidecar_path),
            "trace_event_count": len(trace)
            if isinstance(trace, (list, tuple)) else 0,
            "replay_action_count": len(replay_actions)
            if isinstance(replay_actions, (list, tuple)) else 0,
            "debug_summary": _evaluation_debug_summary(debug),
        })
        externalized.append(item)
    return externalized


def _async_evaluation_worker(request_queue, result_queue, env_factory,
                             fixed_schedule, debug=False):
    """Dedicated evaluation process: build one strict eval env, then score
    each requested policy snapshot with mask-aware episodes.

    Any failure is posted as ``fatal`` and ends the worker — evaluation
    fidelity failures must abort training, exactly like the synchronous
    evaluator this replaces (Tier 3 throughput program, item 5)."""
    torch.set_num_threads(2)
    try:
        configure_runtime_logging(debug=debug, worker=True)
    except Exception:
        pass
    eval_env = None
    try:
        eval_env = env_factory.var()
        fixed_schedule = [dict(case) for case in fixed_schedule]
        n_eval_episodes = len(fixed_schedule)
        if n_eval_episodes <= 0:
            raise RuntimeError("Asynchronous evaluation schedule is empty")
        schedule_hash = evaluation_schedule_sha256(fixed_schedule)
        while True:
            request = request_queue.get()
            if request is None:
                break
            snapshot_path, trigger_timesteps = request
            install_fixed_evaluation_schedule(eval_env, fixed_schedule)
            snapshot_actual = resolve_artifact_path(snapshot_path)
            if snapshot_actual is None:
                raise FileNotFoundError(
                    f"Evaluation snapshot was not published: {snapshot_path}")
            checkpoint_sha256 = sha256_file(snapshot_actual)
            eval_env.env_method(
                "set_evaluation_checkpoint", int(trigger_timesteps),
                checkpoint_sha256)
            model = MaskablePPO.load(
                snapshot_actual, env=eval_env, device="cpu")
            if hasattr(model, "set_random_seed"):
                model.set_random_seed(int(fixed_schedule[0]["seed"]))
            terminal_infos = []

            def capture_terminal_info(callback_locals, _callback_globals):
                if not bool(callback_locals.get("done")):
                    return
                terminal_infos.append(_capture_evaluation_terminal_info(
                    callback_locals.get("info")))

            episode_rewards, episode_lengths = evaluate_policy(
                model, eval_env, n_eval_episodes=n_eval_episodes,
                deterministic=True, return_episode_rewards=True,
                callback=capture_terminal_info)
            if not (len(terminal_infos) == len(episode_rewards)
                    == len(fixed_schedule)):
                raise RuntimeError(
                    "Fixed evaluation completed an unexpected number of "
                    "episodes: outcomes=%s rewards=%s cases=%s" % (
                        len(terminal_infos), len(episode_rewards),
                        len(fixed_schedule)))
            episodes = []
            for case_index, (case, outcome, reward, length) in enumerate(zip(
                    fixed_schedule, terminal_infos, episode_rewards,
                    episode_lengths)):
                expected_case = {
                    key: case.get(key) for key in (
                        "seed", "p1_deck", "p2_deck", "agent_is_p1",
                        "opponent_profile")}
                resolved_case = outcome["resolved_case"]
                if resolved_case != expected_case:
                    raise RuntimeError(
                        "Fixed evaluation case did not resolve as requested: "
                        f"index={case_index} expected={expected_case} "
                        f"resolved={resolved_case}")
                episodes.append(_build_evaluation_episode(
                    case_index, case, outcome, reward, length))
            episodes, summary, promotion_key = \
                summarize_evaluation_episodes(episodes)
            result_queue.put({
                "timesteps": int(trigger_timesteps),
                "snapshot_path": snapshot_path,
                "checkpoint_sha256": checkpoint_sha256,
                "checkpoint_size_bytes": os.path.getsize(snapshot_actual),
                "schedule_sha256": schedule_hash,
                "episodes": episodes,
                "summary": summary,
                "promotion_key": list(promotion_key),
            })
    except Exception:
        result_queue.put({"fatal": traceback.format_exc()})
    finally:
        if eval_env is not None:
            try:
                eval_env.close()
            except Exception:
                pass


class AsyncMaskableEvalCallback(BaseCallback):
    """Periodic mask-aware evaluation in a dedicated process.

    The synchronous evaluator paused every rollout worker for the whole
    evaluation — measured at 73% of wall time at the July 13 defaults
    (Tier 3 throughput program, item 5). This callback saves a policy
    snapshot at each evaluation boundary, hands it to one long-lived
    evaluation process, and folds results into the training logger when
    they arrive (``eval/evaluated_at_timesteps`` records the snapshot's
    true step). Qualified checkpoints are promoted to ``best_model.zip`` by
    decisive outcomes, then timeout avoidance, with shaped return only as the
    final tie-breaker; an unqualified best-so-far candidate is recorded but not
    published. A worker failure fails the run (strict lifecycle); outstanding
    evaluations are awaited at training end so the final policy's score still
    lands in the logs."""

    def __init__(self, eval_env_factory, *, eval_freq, n_eval_episodes,
                 best_model_save_path, snapshot_dir,
                 fixed_evaluation_schedule=None,
                 evaluation_history_path=None, debug=False,
                 minimum_qualification_score=0.55,
                 final_result_timeout_seconds=3600.0, verbose=0):
        super().__init__(verbose)
        self.eval_env_factory = eval_env_factory
        self.eval_freq = int(eval_freq)
        self.n_eval_episodes = int(n_eval_episodes)
        self.best_model_save_path = best_model_save_path
        self.snapshot_dir = snapshot_dir
        self.fixed_evaluation_schedule = [
            dict(case) for case in (fixed_evaluation_schedule or ())]
        if (self.fixed_evaluation_schedule
                and len(self.fixed_evaluation_schedule)
                != self.n_eval_episodes):
            raise ValueError(
                "fixed_evaluation_schedule length must equal n_eval_episodes")
        self.schedule_sha256 = (
            evaluation_schedule_sha256(self.fixed_evaluation_schedule)
            if self.fixed_evaluation_schedule else None)
        self.evaluation_history_path = evaluation_history_path
        self.debug = bool(debug)
        self.final_result_timeout_seconds = float(
            final_result_timeout_seconds)
        self.minimum_qualification_score = float(
            minimum_qualification_score)
        if not 0.0 <= self.minimum_qualification_score <= 1.0:
            raise ValueError(
                "minimum_qualification_score must be between zero and one")
        self.best_mean_reward = -np.inf
        self.best_promotion_key = None
        self.best_candidate_promotion_key = None
        self.best_candidate_timestep = None
        self._evaluation_records = []
        self._skipped_evaluations = []
        self._cancelled_evaluations = []
        self._next_eval_at = None
        self._pending_snapshots = 0
        self._pending_snapshot_paths = {}
        self._process = None
        self._request_queue = None
        self._result_queue = None

    def _on_training_start(self):
        if self.eval_freq <= 0:
            return
        if not self.fixed_evaluation_schedule:
            raise RuntimeError(
                "Periodic evaluation requires a fixed evaluation schedule")
        if not self.evaluation_history_path:
            raise RuntimeError(
                "Periodic evaluation requires an evaluation history path")
        os.makedirs(self.snapshot_dir, exist_ok=True)
        os.makedirs(self.best_model_save_path, exist_ok=True)
        self._write_evaluation_history()
        context = multiprocessing.get_context("spawn")
        self._request_queue = context.Queue()
        self._result_queue = context.Queue()
        self._process = context.Process(
            target=_async_evaluation_worker,
            args=(self._request_queue, self._result_queue,
                  CloudpickleWrapper(self.eval_env_factory),
                  self.fixed_evaluation_schedule, self.debug),
            daemon=True,
            name="async-eval-worker",
        )
        self._process.start()
        self._next_eval_at = self.num_timesteps + self.eval_freq

    def _write_evaluation_history(self):
        if not self.evaluation_history_path:
            return
        promoted = [
            item for item in self._evaluation_records
            if item.get("promoted")]
        write_json_atomic(self.evaluation_history_path, {
            "schema_version": 3,
            "kind": "playersim_fixed_checkpoint_evaluations",
            "schedule_sha256": self.schedule_sha256,
            "fixed_schedule": self.fixed_evaluation_schedule,
            "minimum_qualification_score":
                self.minimum_qualification_score,
            "qualification_rule": {
                "metric": "qualification_interval.lower_bound",
                "operator": ">=",
                "threshold": self.minimum_qualification_score,
                "confidence": 0.95,
            },
            "promotion_order": [
                "decisive_wins",
                "decisive_score",
                "fewer_timeouts",
                "mean_reward",
            ],
            "best_timestep": (
                promoted[-1]["timesteps"] if promoted else None),
            "best_candidate_timestep": self.best_candidate_timestep,
            "skipped_evaluations": self._skipped_evaluations,
            "cancelled_evaluations": self._cancelled_evaluations,
            "evaluations": self._evaluation_records,
        })

    def _handle_result(self, result):
        if "fatal" in result:
            raise RuntimeError(
                "Asynchronous evaluation worker failed:\n" + result["fatal"])
        self._pending_snapshots = max(0, self._pending_snapshots - 1)
        if result.get("schedule_sha256") != self.schedule_sha256:
            raise RuntimeError(
                "Asynchronous evaluation used a different fixed case schedule")
        episodes, summary, promotion_key = summarize_evaluation_episodes(
            result.get("episodes") or ())
        if len(episodes) != self.n_eval_episodes:
            raise RuntimeError(
                "Asynchronous evaluation returned %s episodes for %s cases" % (
                    len(episodes), self.n_eval_episodes))
        for index, episode in enumerate(episodes):
            if episode.get("case") != self.fixed_evaluation_schedule[index]:
                raise RuntimeError(
                    f"Asynchronous evaluation case {index} did not match "
                    "the fixed schedule")
        episodes = _persist_evaluation_debug_sidecars(
            episodes, timestep=result["timesteps"],
            evaluation_history_path=self.evaluation_history_path)
        mean_reward = float(summary["mean_reward"])
        qualification_score = float(summary["qualification_score"])
        qualification_interval = summary["qualification_interval"]
        expected_pair_units = int(summary["episodes"]) // 2
        if (int(summary["episodes"]) % 2
                or qualification_interval.get("paired_units")
                != expected_pair_units):
            raise RuntimeError(
                "Asynchronous evaluation outcomes do not form the complete "
                "adjacent seat-swapped pairs required for qualification")
        qualification_lower = float(
            qualification_interval["lower_bound"])
        qualification_upper = float(
            qualification_interval["upper_bound"])
        logging.info(
            "Async evaluation @ %s steps: decisive=%s-%s score=%s "
            "timeouts=%s/%s mean_reward=%.3f mean_ep_length=%.1f "
            "qualification=%.3f (paired 95%% interval %.3f-%.3f)",
            result["timesteps"], summary["decisive_wins"],
            summary["decisive_losses"], summary["decisive_score"],
            summary["timeouts"], summary["episodes"], mean_reward,
            summary["mean_ep_length"], qualification_score,
            qualification_lower, qualification_upper)
        self.logger.record("eval/mean_reward", mean_reward)
        self.logger.record("eval/std_reward", summary["std_reward"])
        self.logger.record(
            "eval/mean_ep_length", summary["mean_ep_length"])
        self.logger.record(
            "eval/decisive_wins", summary["decisive_wins"])
        self.logger.record(
            "eval/decisive_losses", summary["decisive_losses"])
        self.logger.record(
            "eval/decisive_score", summary["decisive_score"])
        self.logger.record("eval/timeouts", summary["timeouts"])
        self.logger.record("eval/timeout_rate", summary["timeout_rate"])
        qualified = (
            qualification_lower >= self.minimum_qualification_score)
        self.logger.record(
            "eval/qualification_score", qualification_score)
        self.logger.record(
            "eval/qualification_lower_bound", qualification_lower)
        self.logger.record(
            "eval/qualification_upper_bound", qualification_upper)
        self.logger.record("eval/qualified", float(qualified))
        self.logger.record(
            "eval/evaluated_at_timesteps", int(result["timesteps"]))
        snapshot_actual = resolve_artifact_path(result.get("snapshot_path"))
        if snapshot_actual is None:
            raise FileNotFoundError(
                "Evaluated snapshot disappeared before promotion/history")
        checkpoint_sha256 = sha256_file(snapshot_actual)
        reported_sha256 = result.get("checkpoint_sha256")
        if reported_sha256 and reported_sha256 != checkpoint_sha256:
            raise RuntimeError(
                "Evaluated snapshot hash changed between scoring and promotion")
        candidate_promoted = (
            self.best_candidate_promotion_key is None
            or tuple(promotion_key) > self.best_candidate_promotion_key)
        if candidate_promoted:
            self.best_candidate_promotion_key = tuple(promotion_key)
            self.best_candidate_timestep = int(result["timesteps"])
        promoted = qualified and (
            self.best_promotion_key is None
            or tuple(promotion_key) > self.best_promotion_key)
        if promoted:
            self.best_promotion_key = tuple(promotion_key)
            self.best_mean_reward = mean_reward
            best_path = os.path.join(
                self.best_model_save_path, "best_model.zip")
            temporary_best_path = f"{best_path}.tmp"
            try:
                shutil.copyfile(snapshot_actual, temporary_best_path)
                os.replace(temporary_best_path, best_path)
            finally:
                try:
                    os.remove(temporary_best_path)
                except OSError:
                    pass
            logging.info(
                "New qualified fixed-suite outcome key %s (score %.3f, "
                "95%% lower bound %.3f); "
                "promoted the evaluated snapshot to best_model.zip",
                promotion_key, qualification_score, qualification_lower)
        elif candidate_promoted and not qualified:
            logging.warning(
                "Evaluation @ %s is the best candidate so far, but its "
                "qualification 95%% lower bound %.3f (score %.3f) is below "
                "%.3f; best_model.zip "
                "was not published.", result["timesteps"],
                qualification_lower, qualification_score,
                self.minimum_qualification_score)
        self._evaluation_records.append({
            "timesteps": int(result["timesteps"]),
            "completed_at": utc_timestamp(),
            "snapshot_name": os.path.basename(snapshot_actual),
            "checkpoint_sha256": checkpoint_sha256,
            "checkpoint_size_bytes": os.path.getsize(snapshot_actual),
            "schedule_sha256": self.schedule_sha256,
            "promotion_key": list(promotion_key),
            "qualification_score": qualification_score,
            "qualification_interval": qualification_interval,
            "qualified": qualified,
            "candidate_promoted": candidate_promoted,
            "promoted": promoted,
            "summary": summary,
            "episodes": episodes,
        })
        self._write_evaluation_history()
        self._pending_snapshot_paths.pop(snapshot_actual, None)
        try:
            os.remove(snapshot_actual)
        except OSError:
            pass

    def _drain_results(self, timeout=None):
        while True:
            try:
                if timeout is None:
                    result = self._result_queue.get_nowait()
                else:
                    result = self._result_queue.get(timeout=timeout)
                    timeout = None  # Only block while waiting for the first.
            except queue.Empty:
                return
            self._handle_result(result)

    def _record_skipped_evaluation(self, timesteps, reason, outstanding):
        self._skipped_evaluations.append({
            "timesteps": int(timesteps),
            "recorded_at": utc_timestamp(),
            "reason": str(reason),
            "outstanding_evaluations": int(outstanding),
        })
        self._write_evaluation_history()

    def _shutdown_worker(self, join_timeout=30.0):
        if self._process is None:
            return
        try:
            self._request_queue.put(None)
        except Exception:
            pass
        self._process.join(timeout=join_timeout)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=10)
        self._process = None

    def _cleanup_pending_snapshots(self, reason):
        for snapshot_path, timesteps in list(
                self._pending_snapshot_paths.items()):
            self._cancelled_evaluations.append({
                "timesteps": int(timesteps),
                "recorded_at": utc_timestamp(),
                "reason": str(reason),
                "snapshot_name": os.path.basename(snapshot_path),
            })
            try:
                os.remove(snapshot_path)
            except OSError:
                pass
        self._pending_snapshot_paths.clear()
        self._pending_snapshots = 0
        self._write_evaluation_history()

    def cancel_pending(self, reason="run_interrupted"):
        """Stop async evaluation and remove unpublished policy snapshots."""
        if (self._process is None and not self._pending_snapshot_paths
                and self._pending_snapshots <= 0):
            return
        try:
            self._drain_results()
        except Exception as error:
            logging.error(
                "Could not drain evaluation results during cancellation: %s",
                error)
        self._shutdown_worker(join_timeout=5.0)
        try:
            self._drain_results()
        except Exception as error:
            logging.error(
                "Could not process final evaluation result during "
                "cancellation: %s", error)
        self._cleanup_pending_snapshots(reason)

    def _on_step(self):
        if self._process is None:
            return True
        self._drain_results()
        if self._pending_snapshots > 0 and not self._process.is_alive():
            raise RuntimeError(
                "Asynchronous evaluation worker exited with evaluations "
                "outstanding.")
        if self.num_timesteps >= self._next_eval_at:
            if self._pending_snapshots >= 2:
                # The evaluator cannot keep up with the requested cadence.
                # Skip this boundary instead of queueing an unbounded
                # backlog of stale snapshots; the next boundary retries.
                logging.warning(
                    "Async evaluation backlog (%s outstanding); skipping "
                    "the %s-step evaluation.", self._pending_snapshots,
                    self.num_timesteps)
                self._record_skipped_evaluation(
                    self.num_timesteps, "evaluation_backlog",
                    self._pending_snapshots)
            else:
                snapshot_path = os.path.join(
                    self.snapshot_dir,
                    f"eval_snapshot_{self.num_timesteps}_steps")
                self.model.save(snapshot_path)
                snapshot_actual = resolve_artifact_path(snapshot_path)
                if snapshot_actual is None:
                    raise FileNotFoundError(
                        "Evaluation snapshot save did not publish an artifact: "
                        f"{snapshot_path}")
                self._request_queue.put((snapshot_path, self.num_timesteps))
                self._pending_snapshots += 1
                self._pending_snapshot_paths[snapshot_actual] = int(
                    self.num_timesteps)
            while self._next_eval_at <= self.num_timesteps:
                self._next_eval_at += self.eval_freq
        return True

    def _on_training_end(self):
        if self._process is None:
            return
        failure = None
        try:
            deadline = time.time() + self.final_result_timeout_seconds
            while (self._pending_snapshots > 0 and self._process.is_alive()
                   and time.time() < deadline):
                self._drain_results(timeout=5.0)
            if self._pending_snapshots > 0:
                failure = RuntimeError(
                    "%s fixed evaluation result(s) remained outstanding at "
                    "training end; refusing to publish an unevaluated run."
                    % self._pending_snapshots)
        except Exception as error:
            failure = error
        finally:
            self._shutdown_worker()
            if failure is not None:
                self._cleanup_pending_snapshots("training_end_failure")
        if failure is not None:
            raise failure


def cancel_async_evaluations(callbacks, reason):
    """Best-effort cleanup for callbacks when learn() exits exceptionally."""
    for callback in callbacks or ():
        if isinstance(callback, AsyncMaskableEvalCallback):
            try:
                callback.cancel_pending(reason)
            except Exception as error:
                logging.error(
                    "Could not cancel asynchronous evaluation: %s", error)


def create_callbacks(eval_env_factory, run_id, args, num_train_envs=1,
                     tb_run_dir=None, evaluation_schedule=None,
                     curriculum=None):
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
    snapshot_dir = os.path.join(run_model_dir, 'eval_snapshots')
    evaluation_log_dir = os.path.join(LOG_DIR, run_id, 'evaluation')
    for path in (best_model_dir, checkpoint_dir, snapshot_dir,
                 evaluation_log_dir):
        os.makedirs(path, exist_ok=True)

    # Evaluation callback. Runs in its own process; eval_freq stays in
    # total training timesteps (it is compared against num_timesteps, not
    # n_calls, so no per-env division applies).
    eval_callback = AsyncMaskableEvalCallback(
        eval_env_factory,
        eval_freq=args.eval_freq,
        n_eval_episodes=getattr(args, "eval_episodes", 64),
        best_model_save_path=best_model_dir,
        snapshot_dir=snapshot_dir,
        fixed_evaluation_schedule=evaluation_schedule,
        evaluation_history_path=os.path.join(
            evaluation_log_dir, "evaluations.json"),
        debug=getattr(args, "debug", False),
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
    callbacks.append(TrainingProvenanceCallback())
    if curriculum is not None:
        callbacks.append(CurriculumProgressCallback(curriculum))
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
    parser.add_argument(
        "--eval-freq", type=int, default=100000,
        help="Full fixed-suite evaluation frequency in training timesteps")
    parser.add_argument(
        "--eval-episodes", type=int, default=64,
        help=("Fixed paired deck/seat/seed cases per periodic evaluation "
              "(use an even count; 64+ recommended)"))
    parser.add_argument("--checkpoint-freq", type=int, default=50000, help="Checkpoint frequency")
    parser.add_argument("--learning-rate", type=float, default=2e-4, help="Initial learning rate")
    parser.add_argument("--batch-size", type=int, default=256, help="Batch size for training")
    parser.add_argument("--n-steps", type=int, default=1024, help="Number of steps to collect before training")
    parser.add_argument(
        "--n-envs", type=int, default=DEFAULT_TRAINING_ENVIRONMENTS,
        help="Number of environments to run in parallel (0 = auto)")
    parser.add_argument("--debug", action="store_true", help="Enable additional debugging")
    parser.add_argument("--optimize-hp", action="store_true", help="Run hyperparameter optimization")
    parser.add_argument("--record-network", action="store_true", 
                        help="Enable detailed network recording (weights, gradients)")
    parser.add_argument("--record-freq", type=int, default=5000, 
                        help="Frequency for recording network parameters")
    parser.add_argument("--cpu-only", action="store_true", help="Force CPU training even if GPU is available")
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_TRAINING_SEED,
        help="Base seed for Python, NumPy, Torch, and training workers")
    parser.add_argument(
        "--eval-seed", type=int, default=DEFAULT_EVALUATION_SEED,
        help=("Independent seed for evaluation deck selection, paired cases, "
              "and evaluation workers"))
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
    parser.add_argument(
        "--curriculum",
        choices=("combat-v5", "combat-v4", "combat-v3", "combat-v2",
                 "combat-v1", "none"),
        default="combat-v5",
        help="Deterministic training opponent curriculum (evaluation stays fixed)")
    parser.add_argument(
        "--canary-config", choices=tuple(CANARY_CONFIGS), default=None,
        help=("Validate the named canary's enumerated CLI and resolved "
              "experiment contract before training starts"))
    args = parser.parse_args()
    if args.resume and args.optimize_hp:
        parser.error("--resume and --optimize-hp cannot be used together")
    if args.timesteps <= 0:
        parser.error("--timesteps must be positive")
    if args.eval_episodes <= 0:
        parser.error("--eval-episodes must be positive")
    if args.eval_episodes % 2:
        parser.error("--eval-episodes must be even for paired-seat evaluation")
    resume_lineage = None
    if args.resume:
        try:
            resume_lineage = validate_resume_lineage(
                args.resume, args.curriculum)
        except ValueError as error:
            parser.error(str(error))
    # Hyperparameter trials still derive an isolated evaluation seed with the
    # historical offset, while the normal run consumes --eval-seed directly.
    maximum_training_seed = (
        (2**32 - 1) - EVALUATION_SEED_OFFSET - 10_000)
    maximum_evaluation_seed = (2**32 - 1) - 10_000
    if not 0 <= args.seed <= maximum_training_seed:
        parser.error(
            f"--seed must be between 0 and {maximum_training_seed} so "
            "worker and hyperparameter-evaluation seeds remain valid")
    if not 0 <= args.eval_seed <= maximum_evaluation_seed:
        parser.error(
            f"--eval-seed must be between 0 and {maximum_evaluation_seed} so "
            "evaluation worker seeds remain valid")
    try:
        canary_config = validate_canary_cli(args)
    except ValueError as error:
        parser.error(str(error))

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

    run_log_dir = os.path.join(LOG_DIR, run_id)
    run_log_handler = attach_run_log(run_log_dir, debug=args.debug)

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
            "resume_lineage": resume_lineage,
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
                run_log_dir, BASE_DIR).replace(os.sep, "/"),
            "runtime_log": os.path.relpath(
                os.path.join(run_log_dir, "training.log"), BASE_DIR).replace(
                    os.sep, "/"),
            "tensorboard_directory": os.path.relpath(
                tb_run_dir, BASE_DIR).replace(os.sep, "/"),
        },
        "artifacts": {},
        "metrics": {},
        "validation": {"status": "not_run"},
        "failure": None,
        "interruption": None,
    }

    def publish_manifest():
        manifest["timestamps"]["updated_at"] = utc_timestamp()
        write_json_atomic(manifest_path, manifest)

    publish_manifest()

    vec_env = None
    eval_env = None
    model = None
    callbacks = None
    resolved_curriculum = None
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
        resolved_curriculum = resolve_curriculum(args.curriculum, decks)
        num_envs = (
            args.n_envs if args.n_envs > 0
            else max(1, min(6, detected_cpus // 2))
        )
        # A single alternating-seat evaluator avoids global random/NumPy stream
        # coupling between multiple environments inside DummyVecEnv.
        eval_env_count = 1
        eval_seed = args.eval_seed
        eval_rng = random.Random(eval_seed)
        eval_decks = eval_rng.sample(decks, min(10, len(decks)))
        fixed_evaluation_schedule = build_fixed_evaluation_schedule(
            eval_decks, args.eval_episodes, eval_seed)
        fixed_evaluation_schedule_hash = evaluation_schedule_sha256(
            fixed_evaluation_schedule)
        validate_canary_runtime(
            canary_config,
            lineage=lineage,
            training_config=training_config,
            curriculum=resolved_curriculum,
            schedule_sha256=fixed_evaluation_schedule_hash,
            num_envs=num_envs,
            selected_device=selected_device,
        )
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
            "canary_config": json_safe(canary_config),
            "training_config": json_safe(training_config),
            "observation_schema_version":
                AlphaZeroMTGEnv.OBSERVATION_SCHEMA_VERSION,
            "observation_schema_sha256":
                AlphaZeroMTGEnv.OBSERVATION_SCHEMA_SHA256,
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
            "evaluation_vec_env": "DummyVecEnv (dedicated async process)",
            "subprocess_start_method": (
                subproc_start_method if num_envs > 1 else None),
            "learner_threads": learner_threads,
            "selected_device": selected_device,
            "alternate_agent_seat": True,
            "opponent_policy": (
                "scripted-curriculum"
                if resolved_curriculum is not None else "scripted"),
            "curriculum": json_safe(resolved_curriculum),
            "train_matchup_seeds": [
                derive_matchup_seed(args.seed, index)
                for index in range(num_envs)],
            "strategy_memory": "disabled",
            "training_adaptive_decision_history": False,
            "callback_frequencies_timesteps": {
                "evaluation": args.eval_freq,
                "checkpoint": args.checkpoint_freq,
                "network_recording": (
                    args.record_freq if args.record_network else None),
            },
            "evaluation_episodes": args.eval_episodes,
            "fixed_evaluation_schedule_sha256": (
                fixed_evaluation_schedule_hash),
            "fixed_evaluation_schedule": fixed_evaluation_schedule,
            "evaluation_opponent_profile": "scripted",
            "evaluation_curriculum": None,
            "evaluation_adaptive_decision_history": False,
            "training_stats_persistence_interval_games": 10,
            "evaluation_stats_persistence_interval_games": 1,
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
                        'state_potential_scale'],
                    curriculum=resolved_curriculum,
                    opponent_profile="scripted",
                    matchup_seed=derive_matchup_seed(args.seed, idx),
                    stats_persistence_interval_games=10)
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
                        'state_potential_scale'],
                    curriculum=None,
                    opponent_profile="scripted",
                    matchup_seed=eval_seed + idx,
                    adaptive_decision_history_enabled=False,
                    stats_persistence_interval_games=1)
            return _init

        def make_evaluation_vec_env():
            # Built inside the async evaluation worker process (and again in
            # the main process for final checkpoint validation). Construction
            # in the training process would only pay memory for an env the
            # trainer never steps.
            eval_env_fns = [
                make_eval_env_factory(index)
                for index in range(eval_env_count)]
            evaluation_env = StrictEvaluationVecEnv(
                VecMonitor(DummyVecEnv(eval_env_fns)))
            evaluation_env.env_method("set_agent_version", f"{run_id}-eval")
            if hasattr(evaluation_env, "seed"):
                evaluation_env.seed(eval_seed)
            return evaluation_env

        manifest["resolved"]["assigned_evaluation_worker_seeds"] = json_safe(
            [eval_seed + index for index in range(eval_env_count)])

        callbacks = create_callbacks(
            make_evaluation_vec_env, run_id, args, num_train_envs=num_envs,
            tb_run_dir=tb_run_dir,
            evaluation_schedule=fixed_evaluation_schedule,
            curriculum=resolved_curriculum)

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
        if resolved_curriculum is not None:
            if resolved_curriculum.get("progression") == "mastery":
                curriculum_state = getattr(
                    model,
                    CurriculumProgressCallback.MODEL_STATE_ATTRIBUTE, {})
                restored_stage = (
                    int(curriculum_state.get("stage_index", 0))
                    if isinstance(curriculum_state, dict) else 0)
                vec_env.env_method(
                    "set_curriculum_stage", restored_stage,
                    initial_num_timesteps)
            else:
                vec_env.env_method(
                    "set_curriculum_timestep", initial_num_timesteps)
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
            # The evaluation env lives in the async worker during training;
            # build a fresh one here for final checkpoint validation.
            eval_env = make_evaluation_vec_env()
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
            "curriculum_progress": curriculum_progress_manifest(
                model, resolved_curriculum),
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
        cancel_async_evaluations(
            callbacks, "run_interrupted" if was_interrupted else "run_failed")
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
        incomplete_model_save_error = None
        if model is not None:
            incomplete_model_path = os.path.join(
                run_model_dir,
                "interrupted_model" if was_interrupted else "failed_model")
            try:
                pending_or_final = (
                    resolve_artifact_path(os.path.join(run_model_dir, "pending_model"))
                    or resolve_artifact_path(os.path.join(run_model_dir, "final_model")))
                if pending_or_final is not None:
                    incomplete_actual_path = (
                        f"{incomplete_model_path}.zip"
                        if pending_or_final.lower().endswith(".zip")
                        else incomplete_model_path)
                    os.replace(pending_or_final, incomplete_actual_path)
                else:
                    logging.info(
                        "Saving incomplete model to %s",
                        incomplete_model_path)
                    model.save(incomplete_model_path)
            except Exception as save_error:
                incomplete_model_save_error = str(save_error)
                logging.error(
                    f"Could not save incomplete model: {save_error}")
        duration = time.time() - start_time
        actual_num_timesteps = int(getattr(
            model, "num_timesteps", initial_num_timesteps)) \
            if model is not None else initial_num_timesteps
        manifest["metrics"] = {
            "requested_added_timesteps": args.timesteps,
            "initial_timesteps": initial_num_timesteps,
            "final_timesteps": actual_num_timesteps,
            "actual_added_timesteps": max(
                0, actual_num_timesteps - initial_num_timesteps),
            "duration_seconds": duration,
            "evaluation": evaluation_history_summary(run_id),
            "curriculum_progress": curriculum_progress_manifest(
                model, resolved_curriculum),
        }
        manifest["status"] = "interrupted" if was_interrupted else "failed"
        manifest["phase"] = current_phase
        manifest["timestamps"]["finished_at"] = utc_timestamp()
        manifest["timestamps"]["duration_seconds"] = duration
        manifest["artifacts"] = training_artifacts(run_model_dir, run_id)
        incomplete_details = {
            "type": type(e).__name__,
            "message": str(e),
            "phase": current_phase,
            "traceback": failure_traceback,
            "incomplete_model_save_error": incomplete_model_save_error,
        }
        if was_interrupted:
            manifest["interruption"] = incomplete_details
            manifest["failure"] = None
        else:
            manifest["failure"] = incomplete_details
        try:
            publish_manifest()
        except Exception as manifest_error:
            logging.error("Could not publish failure manifest: %s", manifest_error)
        if was_interrupted:
            exit_code = 130
    finally:
        if vec_env is not None:
            try:
                vec_env.close()
            except Exception as close_error:
                # Interrupted SubprocVecEnv workers often raise exceptions
                # whose str() is empty (EOFError, BrokenPipeError); log the
                # repr and traceback so the cause is never a blank line.
                logging.error(
                    "Could not close training environments: %r", close_error)
                logging.debug(
                    "Training environment close failure detail:",
                    exc_info=True)
        if eval_env is not None:
            try:
                eval_env.close()
            except Exception as close_error:
                logging.error(
                    "Could not close evaluation environments: %s", close_error)

    if exit_code == 0:
        logging.info(f"Training run {run_id} completed")
    elif exit_code == 130:
        logging.warning(f"Training run {run_id} was interrupted")
    else:
        logging.error(f"Training run {run_id} failed")
    logging.getLogger().removeHandler(run_log_handler)
    run_log_handler.close()
    return exit_code
    
if __name__ == "__main__":
    # Code to run only when the script is executed directly
    sys.exit(main())
