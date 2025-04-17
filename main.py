import os
import torch
import time
import random
import logging
import argparse
import numpy as np
import traceback
from typing import Dict, List, Type, Union, Optional
import sys
import io

# Stable Baselines and Contrib Imports
from sb3_contrib.ppo_mask import MaskablePPO
import sb3_contrib.common.maskable.policies
from stable_baselines3.common.callbacks import (
    EvalCallback, 
    CheckpointCallback, 
    ProgressBarCallback,
    BaseCallback
)
from stable_baselines3.common.vec_env import DummyVecEnv, VecMonitor
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor
from stable_baselines3.common.evaluation import evaluate_policy
from stable_baselines3.common.env_util import make_vec_env
from stable_baselines3.common.utils import set_random_seed
from sb3_contrib.common.wrappers import ActionMasker
# Optuna for Hyperparameter Optimization
import optuna
# Additional imports for network functionality
import torch.nn.functional as F
import torch.nn as nn

# Import MTG Environment Components
from Playersim.card import load_decks_and_card_db
from Playersim.environment import AlphaZeroMTGEnv
from Playersim.debug import DEBUG_MODE

# Custom Feature Extractor and Policy
class CompletelyFixedMTGExtractor(BaseFeaturesExtractor):
    """
    Features extractor that doesn't rely on CombinedExtractor.
    This provides full control over dimensions and network architecture.
    """
    def __init__(self, observation_space, features_dim=512):
        try:
            super().__init__(observation_space, features_dim=features_dim)
            
            self.output_dim = features_dim
            self.has_initialized = False
            
            # Initialize MLP extractors for each observation key
            self.extractors = {}
            
            # Phase embedding
            self.phase_embedding = torch.nn.Embedding(10, 16)  # Assuming max 10 phases
            
            # Final projections
            self.preprocessing_dim = 256  # Intermediate dimension
            self.final_projection = torch.nn.Sequential(
                torch.nn.Linear(self.preprocessing_dim, self.output_dim),
                torch.nn.ReLU()
            )
            
            # Get the feature dimension from the observation space (more robust)
            card_dim = None
            for key, subspace in observation_space.spaces.items():
                if 'my_hand' in key or 'battlefield' in key:
                    if len(subspace.shape) == 2:
                        _, potential_card_dim = subspace.shape
                        if card_dim is None or potential_card_dim > 0:
                            card_dim = potential_card_dim
                            logging.info(f"Found card dimension {card_dim} from {key}")
                            break
            
            if card_dim is None:
                card_dim = 175  # Fallback to default
                logging.warning(f"Could not determine card dimension from observation space, using fallback: {card_dim}")
            
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
                
                elif len(subspace.shape) == 2:
                    # 2D observations like battlefield and hand
                    n_cards, card_dim_from_space = subspace.shape
                    
                    # Validate card_dim with the one we found earlier
                    if 'my_hand' in key or 'battlefield' in key:
                        if card_dim_from_space != card_dim:
                            logging.warning(f"Dimension mismatch in {key}: Found {card_dim_from_space}, expected {card_dim}")
                    
                    # Ensure we're using the correct dimension
                    actual_card_dim = card_dim_from_space if card_dim_from_space > 0 else card_dim
                    
                    self.extractors[key] = torch.nn.Sequential(
                        torch.nn.Linear(actual_card_dim, 128),
                        torch.nn.ReLU(),
                        torch.nn.Linear(128, 64),
                        torch.nn.ReLU()
                    )
            
            # LSTM for sequential processing
            self.lstm = torch.nn.LSTM(
                input_size=self.output_dim,
                hidden_size=self.output_dim,
                batch_first=True
            )
            
            self.has_initialized = True
            logging.info(f"Feature extractor initialized successfully with output dimension {self.output_dim}")
        except Exception as e:
            logging.error(f"Error initializing feature extractor: {e}")
            logging.error(traceback.format_exc())
            raise
    
    def forward(self, observations):
        """Process the observations through the feature extractors with robust error handling"""
        try:
            encoded_tensor_list = []
                
            # Process discrete observations
            if "phase" in observations:
                phase_tensor = observations["phase"].long()
                phase_emb = self.phase_embedding(phase_tensor)
                encoded_tensor_list.append(phase_emb)
            
            # Process continuous observation spaces
            for key, extractor in self.extractors.items():
                if key in observations:
                    try:
                        # Add dimension check for 2D observations
                        if len(observations[key].shape) == 3 and key in ['my_hand', 'my_battlefield', 'opp_battlefield']:
                            # Shape should be [batch_size, max_cards, feature_dim]
                            logging.debug(f"Observation {key} shape: {observations[key].shape}")
                        
                        extracted = extractor(observations[key])
                        encoded_tensor_list.append(extracted)
                    except Exception as e:
                        logging.error(f"Error processing {key}: {e}")
                        # Continue with other features instead of failing completely
                        continue
            
            if not encoded_tensor_list:
                # If no features could be processed, return zeros
                logging.error("No features could be processed! Returning zeros.")
                return torch.zeros((observations["phase"].shape[0], self.output_dim), 
                                device=observations["phase"].device)
            
            batch_size = encoded_tensor_list[0].shape[0]
            
            # Merge features
            preprocessed_features = torch.cat([tensor.view(batch_size, -1) for tensor in encoded_tensor_list], dim=1)
            
            # Initialize feature_merger if it doesn't exist yet
            if not hasattr(self, "feature_merger"):
                merged_dim = preprocessed_features.shape[1]
                logging.info(f"Initializing feature_merger with input dim {merged_dim} to output dim {self.preprocessing_dim}")
                self.feature_merger = torch.nn.Linear(merged_dim, self.preprocessing_dim).to(preprocessed_features.device)
                # In case we're in inference/evaluation mode and this is still being initialized:
                if not self.training:
                    self.feature_merger.eval()
            
            merged_features = self.feature_merger(preprocessed_features)
            projected_features = self.final_projection(merged_features)
            
            # Add LSTM processing
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
        except Exception as e:
            logging.error(f"Critical error in feature extractor forward: {e}")
            import traceback
            logging.error(traceback.format_exc())
            # Return zeros as fallback with the correct shape
            batch_size = observations["phase"].shape[0] if "phase" in observations else 1
            return torch.zeros((batch_size, self.output_dim), 
                            device=observations["phase"].device if "phase" in observations else "cpu")

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
                return "LSTM Layer for Sequential Processing"
                
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
                    
                # Add LSTM if it exists
                if hasattr(feature_extractor, "lstm"):
                    layers.append(("Feature Extractor", "LSTM Layer"))
                
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
        
        # Check for optional dependencies
        try:
            import psutil
            self.psutil_available = True
        except ImportError:
            logging.warning("psutil not available. CPU and RAM monitoring disabled.")
            
        try:
            import GPUtil
            self.gputil_available = True
        except ImportError:
            logging.warning("GPUtil not available. GPU monitoring disabled.")
    
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
    
    def _on_step(self):
        if self.n_calls % self.monitor_freq == 0:
            # Monitor CPU and RAM
            if self.psutil_available:
                import psutil
                # CPU usage per core
                cpu_percent_per_core = psutil.cpu_percent(percpu=True)
                for i, percent in enumerate(cpu_percent_per_core):
                    self.writer.add_scalar(f"system/cpu_core{i}_percent", percent, self.n_calls)
                
                # Overall CPU usage
                cpu_percent = psutil.cpu_percent()
                self.writer.add_scalar("system/cpu_percent", cpu_percent, self.n_calls)
                
                # RAM usage (GB)
                ram = psutil.virtual_memory()
                ram_used_gb = ram.used / (1024**3)
                ram_percent = ram.percent
                
                self.writer.add_scalar("system/ram_used_gb", ram_used_gb, self.n_calls)
                self.writer.add_scalar("system/ram_percent", ram_percent, self.n_calls)
                
                # Disk usage
                disk = psutil.disk_usage('/')
                disk_percent = disk.percent
                self.writer.add_scalar("system/disk_percent", disk_percent, self.n_calls)
                
                # Network IO
                net_io = psutil.net_io_counters()
                self.writer.add_scalar("system/net_sent_mb", net_io.bytes_sent / (1024**2), self.n_calls)
                self.writer.add_scalar("system/net_recv_mb", net_io.bytes_recv / (1024**2), self.n_calls)
                
                if self.verbose > 0:
                    logging.info(f"Step {self.n_calls}: CPU: {cpu_percent}% RAM: {ram_used_gb:.1f} GB ({ram_percent}%)")
            
            # Monitor PyTorch memory
            if torch.cuda.is_available():
                for i in range(torch.cuda.device_count()):
                    mem_allocated = torch.cuda.memory_allocated(i) / (1024**3)  # GB
                    mem_reserved = torch.cuda.memory_reserved(i) / (1024**3)  # GB
                    
                    self.writer.add_scalar(f"system/cuda{i}_allocated_gb", mem_allocated, self.n_calls)
                    self.writer.add_scalar(f"system/cuda{i}_reserved_gb", mem_reserved, self.n_calls)
                    
                    if self.verbose > 0:
                        logging.info(f"CUDA {i}: Allocated: {mem_allocated:.2f} GB, Reserved: {mem_reserved:.2f} GB")
        
        return True
    
    def _on_training_end(self):
        if self.writer is not None:
            self.writer.close()

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')


# Optimization and Configuration
torch.set_num_threads(os.cpu_count())
torch.set_float32_matmul_precision('high')

# Path Configuration
VERSION = "ALPHA_ZERO_MTG_V3.00"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DECKS_DIR = os.path.join(BASE_DIR, "Decks")
MODEL_DIR = os.path.join(BASE_DIR, "models")
LOG_DIR = os.path.join(BASE_DIR, "logs")
TENSORBOARD_DIR = os.path.join(BASE_DIR, "tensorboard_logs")
# Feature Dimension Configuration
FEATURE_OUTPUT_DIM = 512

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

def objective(trial, main_run_id): # Add main_run_id argument
    """
    Advanced Optuna objective function with more sophisticated parameter space
    and correct environment creation. Now logs under main_run_id.
    """
    logging.info(f"DEBUG: Starting objective function for trial {trial.number} (Main Run: {main_run_id})") # Include main_run_id in log
    # ... (Hyperparameter suggestions remain the same) ...
    learning_rate = trial.suggest_float('learning_rate', 1e-5, 1e-3, log=True)
    n_steps = trial.suggest_categorical('n_steps', [1024, 2048, 4096])
    batch_size = trial.suggest_categorical('batch_size', [64, 128, 256])
    gamma = 1.0 - trial.suggest_float('gamma_complement', 0.0001, 0.1, log=True)
    gae_lambda = trial.suggest_float('gae_lambda', 0.9, 0.999)
    clip_range = trial.suggest_float('clip_range', 0.1, 0.3)
    ent_coef = trial.suggest_float('ent_coef', 1e-5, 0.01, log=True)
    policy_neurons = trial.suggest_categorical('policy_neurons', ['small', 'medium', 'large'])
    network_architectures = {
        'small': {'pi': [128, 64, 32], 'vf': [128, 64, 32]},
        'medium': {'pi': [256, 128, 64], 'vf': [256, 128, 64]},
        'large': {'pi': [512, 256, 128], 'vf': [512, 256, 128]}
    }
    net_arch = network_architectures[policy_neurons]
    n_epochs = trial.suggest_int('n_epochs', 3, 10)
    max_grad_norm = trial.suggest_float('max_grad_norm', 0.3, 0.9)
    activation_name = trial.suggest_categorical('activation_fn', ['relu', 'leaky_relu', 'tanh'])
    activation_fns = {
        'relu': torch.nn.ReLU,
        'leaky_relu': torch.nn.LeakyReLU,
        'tanh': torch.nn.Tanh
    }
    activation_fn = activation_fns[activation_name]

    # --- Deck loading and environment creation (remains the same) ---
    try:
        logging.info(f"Trial {trial.number}: Loading decks and card database...")
        local_decks, local_card_db = load_decks_and_card_db(DECKS_DIR) # Load fresh copy for each trial
        logging.info(f"Trial {trial.number}: Loaded {len(local_decks)} decks with {len(local_card_db)} unique cards")
        try:
            temp_env = AlphaZeroMTGEnv(local_decks, local_card_db)
            feature_dim = getattr(temp_env, '_feature_dim', FEATURE_OUTPUT_DIM)
            logging.info(f"Trial {trial.number}: Fetched feature dimension from temp env: {feature_dim}")
            del temp_env
        except Exception as dim_e:
            logging.error(f"Trial {trial.number}: Failed to get feature dimension from temp env: {dim_e}")
            feature_dim = FEATURE_OUTPUT_DIM
            logging.info(f"Trial {trial.number}: Using fallback feature dimension: {feature_dim}")
    except Exception as e:
        logging.error(f"Trial {trial.number}: Failed to load decks/DB for optimization: {e}")
        logging.error(traceback.format_exc())
        return float('-inf')

    def make_mtg_env():
        env = AlphaZeroMTGEnv(local_decks, local_card_db)
        masked_env = ActionMasker(env, action_mask_fn='action_mask')
        return masked_env

    n_envs = 2
    try:
        logging.info(f"Trial {trial.number}: Creating vectorized environment with {n_envs} envs...")
        vec_env = make_vec_env(make_mtg_env, n_envs=n_envs, vec_env_cls=DummyVecEnv)
        vec_env = VecMonitor(vec_env)
        logging.info(f"Trial {trial.number}: Created vectorized environment with ActionMasker")
    except ValueError as ve:
        logging.error(f"Trial {trial.number}: ValueError creating vec_env (check make_mtg_env?): {ve}")
        logging.error(traceback.format_exc())
        return float('-inf')
    except Exception as e:
        logging.error(f"Trial {trial.number}: Failed to create environment: {e}")
        logging.error(traceback.format_exc())
        return float('-inf')

    # --- Policy and Model Creation (remains the same) ---
    try:
        policy_kwargs = {
            "features_extractor_class": CompletelyFixedMTGExtractor,
            "features_extractor_kwargs": {
                "features_dim": FEATURE_OUTPUT_DIM # Use the globally determined/fallback feature dim
            },
            "net_arch": net_arch,
            "activation_fn": activation_fn
        }
        logging.info(f"Trial {trial.number}: Configured policy with feature dimension {FEATURE_OUTPUT_DIM}")
    except Exception as e:
        logging.error(f"Trial {trial.number}: Error configuring policy: {e}")
        logging.error(traceback.format_exc())
        vec_env.close()
        return float('-inf')

    try:
        logging.info(f"Trial {trial.number}: Creating model...")
        model = MaskablePPO(
            policy=FixedDimensionMaskableActorCriticPolicy,
            env=vec_env,
            learning_rate=learning_rate,
            n_steps=n_steps,
            batch_size=batch_size,
            gamma=gamma,
            gae_lambda=gae_lambda,
            clip_range=clip_range,
            ent_coef=ent_coef,
            policy_kwargs=policy_kwargs,
            verbose=0,
            tensorboard_log=TENSORBOARD_DIR, # Base TB directory
            n_epochs=n_epochs,
            max_grad_norm=max_grad_norm
        )
        logging.info(f"Trial {trial.number}: Model created successfully")
    except Exception as e:
        logging.error(f"Trial {trial.number}: Error creating model: {e}")
        logging.error(traceback.format_exc())
        vec_env.close()
        return float('-inf')

    # --- Training Loop: Update tb_log_name ---
    try:
        logging.info(f"Trial {trial.number}: Starting evaluation steps...")
        total_timesteps_per_trial = 100000
        n_eval_points = 5
        step_size = total_timesteps_per_trial // n_eval_points

        # *** THE KEY CHANGE: Construct the tb_log_name using main_run_id ***
        tb_log_name_for_trial = f"{main_run_id}/trial_{trial.number}"
        # Use this combined name for logging within this trial
        logging.info(f"Trial {trial.number}: Logging TensorBoard data to sub-path: {tb_log_name_for_trial}")

        for step in range(n_eval_points):
            logging.info(f"Trial {trial.number}: Training step {step+1}/{n_eval_points}, {step_size} timesteps...")
            current_logger_level = getattr(model.logger, 'level', 0) if hasattr(model, 'logger') else 0

            # *** Use the combined tb_log_name here ***
            model.learn(total_timesteps=step_size, reset_num_timesteps=(step==0), tb_log_name=tb_log_name_for_trial, log_interval=max(1, step_size // 1000))

            # --- Evaluation part remains the same ---
            if hasattr(model, 'logger'): model.logger.set_level(logging.WARN)
            logging.info(f"Trial {trial.number}: Evaluating model performance at step {(step+1)*step_size}...")
            def make_eval_mtg_env():
                env = AlphaZeroMTGEnv(local_decks, local_card_db)
                return ActionMasker(env, action_mask_fn='action_mask')
            n_eval_envs = max(1, n_envs // 2)
            temp_eval_env = make_vec_env(make_eval_mtg_env, n_envs=n_eval_envs, vec_env_cls=DummyVecEnv)
            temp_eval_env = VecMonitor(temp_eval_env)
            try:
                mean_reward, std_reward = evaluate_policy(model, temp_eval_env, n_eval_episodes=5, warn=False)
                logging.info(f"Trial {trial.number}: Step {step+1} evaluation - Mean reward: {mean_reward:.4f}, Std: {std_reward:.4f}")
            except Exception as eval_e:
                 logging.error(f"Trial {trial.number}: Error during evaluation: {eval_e}")
                 mean_reward = float('-inf')
            finally:
                 temp_eval_env.close()
                 del temp_eval_env
            if hasattr(model, 'logger'): model.logger.set_level(current_logger_level)
            trial.report(mean_reward, step)
            if trial.should_prune():
                logging.info(f"Trial {trial.number}: Pruned at step {step+1} with mean reward {mean_reward:.4f}")
                raise optuna.TrialPruned()

        logging.info(f"Trial {trial.number}: Completed successfully with final reported reward {mean_reward:.4f}")
        return mean_reward
    except optuna.TrialPruned:
        logging.info(f"Trial {trial.number}: Trial pruned due to poor performance")
        raise
    except Exception as e:
        logging.error(f"Trial {trial.number}: Hyperparameter trial failed during training/evaluation: {e}")
        logging.error(traceback.format_exc())
        return float('-inf')
    finally:
        logging.info(f"Trial {trial.number}: Cleaning up training environment")
        try:
            if 'vec_env' in locals() and vec_env is not None:
                 vec_env.close()
                 del vec_env
        except Exception as close_e:
            logging.error(f"Trial {trial.number}: Error closing training environment: {close_e}")
        if 'model' in locals() and model is not None:
             del model
        if torch.cuda.is_available(): torch.cuda.empty_cache()

def optimize_hyperparameters(main_run_id, n_trials=50, study_name="mtg_optimization"): # Add main_run_id argument
    """Run Optuna hyperparameter optimization with persistence and pruning"""
    logging.info(f"Starting hyperparameter optimization with {n_trials} trials... (Run ID: {main_run_id})") # Log the main run ID

    try:
        storage_name = f"sqlite:///{study_name}.db"
        logging.info(f"Using storage: {storage_name}")

        study = optuna.create_study(
            study_name=study_name,
            storage=storage_name,
            load_if_exists=True,
            direction='maximize',
            pruner=optuna.pruners.MedianPruner()  # Early stopping for bad trials
        )

        logging.info("Study created successfully. Starting optimization...")
        # Use functools.partial to pass main_run_id to the objective function
        import functools
        objective_with_id = functools.partial(objective, main_run_id=main_run_id)
        # Pass the wrapped objective function to optimize
        study.optimize(objective_with_id, n_trials=n_trials) # Use the partial function

        logging.info(f"Optimization completed with {len(study.trials)} trials for run {main_run_id}")

        # --- Visualization part remains the same ---
        try:
            import matplotlib.pyplot as plt

            # Create optimization plots directory
            plots_dir = os.path.join(BASE_DIR, "optimization_plots", main_run_id) # Store plots under run_id folder
            os.makedirs(plots_dir, exist_ok=True)

            # Plot optimization history
            plt.figure(figsize=(10, 6))
            optuna.visualization.matplotlib.plot_optimization_history(study)
            plt.title(f"Optimization History ({main_run_id})") # Add run_id to title
            plt.savefig(os.path.join(plots_dir, f"{study_name}_history.png"))
            plt.close() # Close the figure

            # Plot parameter importances
            plt.figure(figsize=(10, 6))
            optuna.visualization.matplotlib.plot_param_importances(study)
            plt.title(f"Parameter Importances ({main_run_id})") # Add run_id to title
            plt.savefig(os.path.join(plots_dir, f"{study_name}_importances.png"))
            plt.close() # Close the figure

            logging.info(f"Optimization plots saved to {plots_dir}")
        except Exception as e:
            logging.warning(f"Could not create optimization plots: {e}")
            logging.warning(traceback.format_exc())

        logging.info(f"Best parameters for {main_run_id}: {study.best_params}")
        return study.best_params
    except Exception as e:
        logging.error(f"Error during hyperparameter optimization for run {main_run_id}: {e}")
        logging.error(traceback.format_exc())
        raise

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
    architecture_dir = os.path.join(MODEL_DIR, f"{run_id}_architecture")
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

def create_callbacks(eval_env, run_id, args):
    """Create a comprehensive set of callbacks"""
    # Evaluation callback
    eval_callback = EvalCallback(
        eval_env=eval_env,
        best_model_save_path=MODEL_DIR,
        log_path=LOG_DIR,
        eval_freq=args.eval_freq,
        deterministic=False,
        n_eval_episodes=20
    )

    # Checkpoint callback
    checkpoint_callback = CheckpointCallback(
        save_freq=args.checkpoint_freq,
        save_path=MODEL_DIR,
        name_prefix=f"ppo_mtg_{run_id}"
    )

    # Progress bar callback
    progress_callback = ProgressBarCallback()
    
    # Network recording callback
    network_callback = NetworkRecordingCallback(
        log_dir=os.path.join(TENSORBOARD_DIR, f"network_logs_{run_id}"),
        record_freq=5000  # Record weights every 5000 steps
    )
    
    # Resource monitoring callback
    resource_callback = ResourceMonitorCallback(
        log_dir=os.path.join(TENSORBOARD_DIR, f"system_logs_{run_id}"),
        monitor_freq=5000
    )

    return [eval_callback, checkpoint_callback, progress_callback, network_callback, resource_callback]

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
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Train an MTG AI agent")
    parser.add_argument("--resume", type=str, help="Path to a model to resume training from")
    parser.add_argument("--timesteps", type=int, default=1000000, help="Total timesteps to train")
    parser.add_argument("--eval-freq", type=int, default=10000, help="Evaluation frequency")
    parser.add_argument("--checkpoint-freq", type=int, default=50000, help="Checkpoint frequency")
    parser.add_argument("--learning-rate", type=float, default=3e-4, help="Initial learning rate")
    parser.add_argument("--batch-size", type=int, default=256, help="Batch size for training")
    parser.add_argument("--n-steps", type=int, default=2048, help="Number of steps to collect before training")
    parser.add_argument("--n-envs", type=int, default=0, help="Number of environments to run in parallel (0 = auto)")
    parser.add_argument("--debug", action="store_true", help="Enable additional debugging")
    parser.add_argument("--optimize-hp", action="store_true", help="Run hyperparameter optimization")
    parser.add_argument("--record-network", action="store_true",
                        help="Enable detailed network recording (weights, gradients)")
    parser.add_argument("--record-freq", type=int, default=5000,
                        help="Frequency for recording network parameters")
    parser.add_argument("--cpu-only", action="store_true", help="Force CPU training even if GPU is available")
    parser.add_argument("--trial-limit", type=int, default=50,
                        help="Limit the number of trials for hyperparameter optimization")
    args = parser.parse_args()

    try:
        # Ensure UTF-8 for stdout/stderr EARLY
        try:
            # Only wrap if not already wrapped (prevents errors in some environments)
            if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding.lower() != 'utf-8':
                 sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
            if not isinstance(sys.stderr, io.TextIOWrapper) or sys.stderr.encoding.lower() != 'utf-8':
                 sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
        except Exception as io_err:
             print(f"Warning: Could not wrap stdout/stderr with UTF-8: {io_err}", file=sys.stderr)

        # Set random seed for reproducibility
        set_random_seed(42)

        # Create required directories
        os.makedirs(MODEL_DIR, exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)
        os.makedirs(TENSORBOARD_DIR, exist_ok=True)

        # Create a unique run ID with timestamp
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        run_id = f"{VERSION}_{timestamp}"

        # --- CONFIGURE LOGGING HERE ---
        log_level = logging.DEBUG if args.debug else logging.INFO
        log_filename = os.path.join(LOG_DIR, f"{run_id}.log") # Use run_id for filename
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
            handler.close()
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s [%(name)s] - %(message)s',
            handlers=[
                logging.FileHandler(log_filename, encoding='utf-8', errors='replace'),
                logging.StreamHandler(sys.stdout)
            ]
        )
        logging.info(f"Logging configured. Log file: {log_filename}")
        # --- END LOGGING CONFIGURATION ---

        if args.debug:
            global DEBUG_MODE
            DEBUG_MODE = True
            logging.debug("Debug mode enabled.")

        if args.cpu_only:
            os.environ["CUDA_VISIBLE_DEVICES"] = ""
            logging.info("Running in CPU-only mode as requested")

        n_cpu_threads = min(10, os.cpu_count() or 1)
        torch.set_num_threads(n_cpu_threads)
        logging.info(f"PyTorch using {n_cpu_threads} CPU threads")

        if torch.cuda.is_available() and not args.cpu_only:
            device_count = torch.cuda.device_count()
            device_names = [torch.cuda.get_device_name(i) for i in range(device_count)]
            logging.info(f"Using {device_count} GPU(s): {device_names}")
        else:
            logging.info("Using CPU for training")

        logging.info("Loading decks and card database...")
        try:
            decks, card_db = load_decks_and_card_db(DECKS_DIR)
            logging.info(f"Loaded {len(decks)} decks with {len(card_db)} unique cards")
            try:
                temp_env = ActionMasker(AlphaZeroMTGEnv(decks, card_db), action_mask_fn='action_mask')
                feature_dim = getattr(temp_env.env, '_feature_dim', 175)
                temp_env.close()
                del temp_env
            except Exception as temp_env_err:
                 logging.warning(f"Could not create temp env to get feature dim: {temp_env_err}. Using default.")
                 feature_dim = 175
            logging.info(f"Using feature dimension: {feature_dim}")
            global FEATURE_OUTPUT_DIM
            FEATURE_OUTPUT_DIM = feature_dim
            logging.info(f"Updated FEATURE_OUTPUT_DIM to {FEATURE_OUTPUT_DIM}")
        except Exception as e:
            logging.error(f"Failed to load decks: {str(e)}", exc_info=True)
            return

        # --- Optional Hyperparameter Optimization ---
        # Initialize best_params *before* the if block
        best_params = {}
        if args.optimize_hp:
            import psutil
            cpu_count = psutil.cpu_count(logical=True) or 1

            if cpu_count <= 4:
                n_trials = min(10, args.trial_limit)
                logging.info(f"Limited CPU resources ({cpu_count} logical cores). Running light optimization: {n_trials} trials")
            elif cpu_count <= 8:
                n_trials = min(25, args.trial_limit)
                logging.info(f"Moderate CPU resources ({cpu_count} logical cores). Running standard optimization: {n_trials} trials")
            else:
                n_trials = min(args.trial_limit, 50)
                logging.info(f"Good CPU resources ({cpu_count} logical cores). Running optimization: {n_trials} trials")

            logging.info("Starting hyperparameter optimization...")
            try:
                study_name = f"mtg_opt_{run_id}"
                # *** Pass the main run_id to the optimization function ***
                best_params = optimize_hyperparameters(run_id, n_trials=n_trials, study_name=study_name)
                logging.info("Hyperparameter optimization completed. Best params:")
                for key, value in best_params.items():
                    logging.info(f"  {key}: {value}")
                    # Update args with best params
                    if key == 'gamma_complement':
                         # We don't have a gamma arg, so store it for ppo_params later
                         pass # We'll use best_params['gamma_complement'] directly later
                    elif hasattr(args, key):
                         setattr(args, key, value)
                    elif hasattr(args, key.replace('_', '-')):
                         setattr(args, key.replace('_', '-'), value)
                    else:
                         logging.warning(f"Could not map optimized parameter '{key}' to args.")
            except Exception as e:
                logging.error(f"Hyperparameter optimization failed: {e}", exc_info=True)
                return

        # Determine number of environments
        if torch.cuda.is_available() and not args.cpu_only:
             default_envs = min(12, os.cpu_count() or 1)
        else:
             default_envs = min(6, (os.cpu_count() or 2) // 2)
             default_envs = max(1, default_envs)

        num_envs = args.n_envs if args.n_envs > 0 else default_envs
        logging.info(f"Creating {num_envs} parallel environments")

        try:
            env_fns = [
                lambda: ActionMasker(
                    AlphaZeroMTGEnv(decks, card_db),
                    action_mask_fn='action_mask'
                ) for _ in range(num_envs)
            ]
            vec_env = DummyVecEnv(env_fns)
            vec_env = VecMonitor(vec_env)
            logging.info("Training environment created successfully.")
        except Exception as e:
            logging.error(f"Failed to create training environment: {e}", exc_info=True)
            return

        lr_scheduler = CustomLearningRateScheduler(
            initial_lr=args.learning_rate # Use potentially optimized LR from args
        )

        # --- Use potentially optimized architecture/activation from best_params ---
        policy_net_arch = { "pi": [256, 128, 64], "vf": [256, 128, 64] } # Default
        policy_activation_fn = torch.nn.ReLU # Default
        if 'policy_neurons' in best_params:
             policy_neurons = best_params['policy_neurons']
             network_architectures = {
                 'small': {'pi': [128, 64, 32], 'vf': [128, 64, 32]},
                 'medium': {'pi': [256, 128, 64], 'vf': [256, 128, 64]},
                 'large': {'pi': [512, 256, 128], 'vf': [512, 256, 128]}
             }
             policy_net_arch = network_architectures.get(policy_neurons, policy_net_arch)
             logging.info(f"Using optimized network architecture: {policy_neurons}")
        if 'activation_fn' in best_params:
            activation_name = best_params['activation_fn']
            activation_fns = { 'relu': torch.nn.ReLU, 'leaky_relu': torch.nn.LeakyReLU, 'tanh': torch.nn.Tanh }
            policy_activation_fn = activation_fns.get(activation_name, policy_activation_fn)
            logging.info(f"Using optimized activation function: {activation_name}")
        # --- End Optimized Arch/Activation ---

        policy_kwargs = {
            "features_extractor_class": CompletelyFixedMTGExtractor,
            "features_extractor_kwargs": {
                "features_dim": FEATURE_OUTPUT_DIM
            },
            "net_arch": policy_net_arch,
            "activation_fn": policy_activation_fn
        }

        # Create evaluation environment
        eval_decks_sample_size = min(10, len(decks))
        eval_envs_count = max(1, num_envs // 2)
        logging.info(f"Creating {eval_envs_count} evaluation environments with {eval_decks_sample_size} decks.")
        try:
            eval_decks = random.sample(decks, eval_decks_sample_size)
            eval_env_fns = [
                lambda: ActionMasker(
                    AlphaZeroMTGEnv(eval_decks, card_db),
                    action_mask_fn='action_mask'
                ) for _ in range(eval_envs_count)
            ]
            eval_env = VecMonitor(DummyVecEnv(eval_env_fns))
            logging.info("Evaluation environment created successfully.")
        except Exception as e:
            logging.error(f"Failed to create evaluation environment: {e}", exc_info=True)
            vec_env.close()
            return

        # Create callbacks
        callbacks = create_callbacks(eval_env, run_id, args)
        if args.record_network:
             network_cb = NetworkRecordingCallback(
                 log_dir=os.path.join(TENSORBOARD_DIR, f"{run_id}/network_logs"), # Log under run_id subfolder
                 record_freq=args.record_freq
             )
             callbacks.append(network_cb)
             logging.info(f"Network recording enabled (freq: {args.record_freq}).")
        perf_cb = TrainingPerformanceCallback(
            log_dir=os.path.join(TENSORBOARD_DIR, f"{run_id}/performance_logs"), # Log under run_id subfolder
            monitor_freq=1000
        )
        callbacks.append(perf_cb)
        res_cb = ResourceMonitorCallback(
            log_dir=os.path.join(TENSORBOARD_DIR, f"{run_id}/system_logs"), # Log under run_id subfolder
            monitor_freq=5000
        )
        callbacks.append(res_cb)
        logging.info("Performance and resource monitoring callbacks added.")

        start_time = time.time()
        model = None

        try:
            # --- Model hyperparameters: Prioritize Optuna results over args ---
            ppo_params = {
                 "policy": FixedDimensionMaskableActorCriticPolicy,
                 "env": vec_env,
                 "learning_rate": lr_scheduler,
                 "n_steps": best_params.get('n_steps', args.n_steps),
                 "batch_size": best_params.get('batch_size', args.batch_size),
                 "n_epochs": best_params.get('n_epochs', 5),
                 # Calculate gamma from complement if optimized
                 "gamma": 1.0 - best_params['gamma_complement'] if 'gamma_complement' in best_params else getattr(args, 'gamma', 0.995),
                 "gae_lambda": best_params.get('gae_lambda', 0.95),
                 "clip_range": best_params.get('clip_range', 0.2),
                 "ent_coef": best_params.get('ent_coef', 0.01),
                 "max_grad_norm": best_params.get('max_grad_norm', 0.5),
                 "verbose": 1,
                 "tensorboard_log": TENSORBOARD_DIR,
                 "policy_kwargs": policy_kwargs,
                 "device": "cuda" if torch.cuda.is_available() and not args.cpu_only else "cpu"
            }
            logging.info(f"Using device: {ppo_params['device']}")
            logging.info(f"PPO Parameters: { {k:v for k,v in ppo_params.items() if k != 'policy_kwargs'} }") # Log params excluding complex kwargs

            if args.resume and os.path.exists(f"{args.resume}.zip"):
                 logging.info(f"Resuming training from {args.resume}.zip")
                 model = MaskablePPO.load(
                     args.resume,
                     env=vec_env,
                     tensorboard_log=TENSORBOARD_DIR,
                     # Re-apply potentially optimized parameters when resuming
                     learning_rate=lr_scheduler,
                     n_steps=ppo_params['n_steps'],
                     batch_size=ppo_params['batch_size'],
                     n_epochs=ppo_params['n_epochs'],
                     gamma=ppo_params['gamma'],
                     gae_lambda=ppo_params['gae_lambda'],
                     clip_range=ppo_params['clip_range'],
                     ent_coef=ppo_params['ent_coef'],
                     max_grad_norm=ppo_params['max_grad_norm'],
                     device=ppo_params['device'],
                     policy_kwargs=policy_kwargs
                 )
            else:
                 if args.resume:
                     logging.warning(f"Resume path {args.resume}.zip not found. Starting new training.")
                 logging.info("Creating new MaskablePPO model.")
                 model = MaskablePPO(**ppo_params)

            logging.info(f"Starting training run: {run_id} for {args.timesteps} timesteps.")
            model.learn(
                total_timesteps=args.timesteps,
                callback=callbacks,
                tb_log_name=run_id, # <-- Log main training under run_id directly
                reset_num_timesteps=not args.resume
            )

            training_duration = time.time() - start_time
            hours, remainder = divmod(training_duration, 3600)
            minutes, seconds = divmod(remainder, 60)
            logging.info(f"Training completed in {int(hours)}h {int(minutes)}m {int(seconds)}s")

            if model:
                logging.info("Recording final neural network architecture...")
                record_network_architecture(model, run_id)

        except KeyboardInterrupt:
             logging.warning("Training interrupted by user.")
        except Exception as e:
            logging.error(f"Training error: {str(e)}", exc_info=True)
        finally:
            if model is not None:
                final_model_path = os.path.join(MODEL_DIR, f"{run_id}_final")
                logging.info(f"Saving final model to {final_model_path}.zip")
                model.save(final_model_path)

                if hasattr(model, "policy"):
                    if hasattr(model.policy, "features_extractor"):
                        feature_extractor_path = os.path.join(MODEL_DIR, f"{run_id}_feature_extractor.pth")
                        torch.save(model.policy.features_extractor.state_dict(), feature_extractor_path)
                        logging.info(f"Saved feature extractor state dict to {feature_extractor_path}")
                    if hasattr(model.policy, "mlp_extractor"):
                         policy_net_path = os.path.join(MODEL_DIR, f"{run_id}_policy_network.pth")
                         torch.save(model.policy.mlp_extractor.state_dict(), policy_net_path)
                         logging.info(f"Saved policy network state dict to {policy_net_path}")

            logging.info("Closing environments...")
            if 'vec_env' in locals() and vec_env is not None:
                try: vec_env.close()
                except Exception as env_close_err: logging.warning(f"Error closing training env: {env_close_err}")
            if 'eval_env' in locals() and eval_env is not None:
                try: eval_env.close()
                except Exception as env_close_err: logging.warning(f"Error closing eval env: {env_close_err}")

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                logging.info("Cleared CUDA cache.")

            logging.info(f"Training run {run_id} finished.")

    except Exception as e:
        try:
            logging.critical(f"Critical error in main execution: {e}", exc_info=True)
        except NameError:
             print(f"CRITICAL ERROR in main (logging unavailable): {e}\n{traceback.format_exc()}", file=sys.stderr)
        return 1
    return 0
    
if __name__ == "__main__":
    # Code to run only when the script is executed directly
    main()