#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""The Connectionist Temporal Classification model."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

try:
    from warpctc_pytorch import CTCLoss
except ImportError:
    raise ImportError('Install warpctc_pytorch.')
try:
    import pytorch_ctc
except ImportError:
    raise ImportError('Install pytorch_ctc.')

import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F

from models.pytorch.base import ModelBase
from models.pytorch.linear import LinearND
from models.pytorch.encoders.load_encoder import load
from models.pytorch.ctc.decoders.greedy_decoder import GreedyDecoder
from models.pytorch.ctc.decoders.beam_search_decoder import BeamSearchDecoder
# from models.pytorch.ctc.decoders.beam_search_decoder2 import BeamSearchDecoder
from utils.io.variable import np2var, var2np

NEG_INF = -float("inf")
LOG_0 = NEG_INF
LOG_1 = 0


class CTC(ModelBase):
    """The Connectionist Temporal Classification model.
    Args:
        input_size (int): the dimension of input features
        encoder_type (string): the type of the encoder. Set lstm or gru or rnn.
        bidirectional (bool): if True create a bidirectional encoder
        num_units (int): the number of units in each layer
        num_proj (int): the number of nodes in recurrent projection layer
        num_layers (int): the number of layers of the encoder
        fc_list (list):
        dropout (float): the probability to drop nodes
        num_classes (int): the number of classes of target labels
            (excluding a blank class)
        parameter_init (float, optional): Range of uniform distribution to
            initialize weight parameters
        subsample_list (list, optional): subsample in the corresponding layers (True)
            ex.) [False, True, True, False] means that subsample is conducted
                in the 2nd and 3rd layers.
        logits_temperature (float):
        num_stack (int, optional): the number of frames to stack
        splice (int, optional): frames to splice. Default is 1 frame.
        conv_channels (list, optional):
        conv_kernel_sizes (list, optional):
        conv_strides (list, optional):
        poolings (list, optional):
        activation (string, optional): The activation function of CNN layers.
            Choose from relu or prelu or hard_tanh or maxout
        batch_norm (bool, optional):
        weight_noise_std (float, optional):
        residual (bool, optional):
        dense_residual (bool, optional):
    """

    def __init__(self,
                 input_size,
                 encoder_type,
                 bidirectional,
                 num_units,
                 num_proj,
                 num_layers,
                 fc_list,
                 dropout,
                 num_classes,
                 parameter_init=0.1,
                 subsample_list=[],
                 logits_temperature=1,
                 num_stack=1,
                 splice=1,
                 conv_channels=[],
                 conv_kernel_sizes=[],
                 conv_strides=[],
                 poolings=[],
                 activation='relu',
                 batch_norm=False,
                 weight_noise_std=0,
                 residual=False,
                 dense_residual=False):

        super(ModelBase, self).__init__()

        self.encoder_type = encoder_type
        self.num_directions = 2 if bidirectional else 1
        self.fc_list = fc_list
        self.subsample_list = subsample_list

        # Setting for CTC
        self.num_classes = num_classes + 1
        # NOTE: Add blank class
        self.blank_index = 0
        # NOTE: index 0 is reserved for blank in warpctc_pytorch
        self.logits_temperature = logits_temperature

        # Regualarization
        self.parameter_init = parameter_init
        self.weight_noise_injection = False
        self.weight_noise_std = float(weight_noise_std)

        # Load an instance
        if sum(subsample_list) == 0:
            encoder = load(encoder_type=encoder_type)
        else:
            encoder = load(encoder_type='p' + encoder_type)

        # Call the encoder function
        if encoder_type in ['lstm', 'gru', 'rnn']:
            if sum(subsample_list) == 0:
                self.encoder = encoder(
                    input_size=input_size,  # 120 or 123
                    rnn_type=encoder_type,
                    bidirectional=bidirectional,
                    num_units=num_units,
                    num_proj=num_proj,
                    num_layers=num_layers,
                    dropout=dropout,
                    parameter_init=parameter_init,
                    use_cuda=self.use_cuda,
                    batch_first=False,
                    num_stack=num_stack,
                    splice=splice,
                    conv_channels=conv_channels,
                    conv_kernel_sizes=conv_kernel_sizes,
                    conv_strides=conv_strides,
                    poolings=poolings,
                    activation=activation,
                    batch_norm=batch_norm,
                    residual=residual,
                    dense_residual=dense_residual)
            else:
                # Pyramidal encoder
                self.encoder = encoder(
                    input_size=input_size,  # 120 or 123
                    rnn_type=encoder_type,
                    bidirectional=bidirectional,
                    num_units=num_units,
                    num_proj=num_proj,
                    num_layers=num_layers,
                    dropout=dropout,
                    parameter_init=parameter_init,
                    subsample_list=subsample_list,
                    subsample_type='concat',
                    use_cuda=self.use_cuda,
                    batch_first=False,
                    num_stack=num_stack,
                    splice=splice,
                    conv_channels=conv_channels,
                    conv_kernel_sizes=conv_kernel_sizes,
                    conv_strides=conv_strides,
                    poolings=poolings,
                    activation=activation,
                    batch_norm=batch_norm,
                    residual=residual,
                    dense_residual=dense_residual)

        elif encoder_type == 'cnn':
            assert num_stack == 1
            assert splice == 1
            self.encoder = encoder(
                input_size=input_size,  # 120 or 123
                conv_channels=conv_channels,
                conv_kernel_sizes=conv_kernel_sizes,
                conv_strides=conv_strides,
                poolings=poolings,
                dropout=dropout,
                parameter_init=parameter_init,
                activation=activation,
                use_cuda=self.use_cuda,
                batch_norm=batch_norm)
        else:
            raise NotImplementedError

        if len(fc_list) > 0:
            fc_layers = []
            for i in range(len(fc_list)):
                if i == 0:
                    if encoder_type == 'cnn':
                        bottle_input_size = self.encoder.output_size
                    else:
                        bottle_input_size = num_units * self.num_directions
                    # if batch_norm:
                    #     fc_layers.append(nn.BatchNorm1d(bottle_input_size))
                    fc_layers.append(LinearND(bottle_input_size, fc_list[i]))
                else:
                    # if batch_norm:
                    #     fc_layers.append(nn.BatchNorm1d(fc_list[i - 1]))
                    fc_layers.append(LinearND(fc_list[i - 1], fc_list[i]))
                fc_layers.append(nn.Dropout(p=dropout))
            self.fc_layers = nn.Sequential(*fc_layers)
            self.fc = LinearND(fc_list[-1], self.num_classes)
        else:
            self.fc = LinearND(
                num_units * self.num_directions, self.num_classes)

        # Set CTC decoders
        self._decode_greedy_np = GreedyDecoder(blank_index=self.blank_index)
        self._decode_beam_np = BeamSearchDecoder(blank_index=self.blank_index)
        # TODO: set space index

    def forward(self, inputs, labels, inputs_seq_len, labels_seq_len,
                is_eval=False):
        """Forward computation (only training).
        Args:
            inputs (np.ndarray): A tensor of size `[B, T_in, input_size]`
            labels (np.ndarray): A tensor of size `[B, T_out]`
            inputs_seq_len (np.ndarray): A tensor of size `[B]`
            labels_seq_len (np.ndarray): A tensor of size `[B]`
            is_eval (bool): if True, the history will not be saved.
                This should be used in inference model for memory efficiency.
        Returns:
            loss (FloatTensor): A tensor of size `[1]`
        """
        # Wrap by Variable
        inputs_var = np2var(inputs, use_cuda=self.use_cuda)
        labels_var = np2var(labels, dtype='int', use_cuda=False)
        inputs_seq_len_var = np2var(
            inputs_seq_len, dtype='int', use_cuda=self.use_cuda)
        labels_seq_len_var = np2var(
            labels_seq_len, dtype='int', use_cuda=False)

        # NOTE: index 0 is reserved for blank in warpctc_pytorch
        labels_var = labels_var + 1

        if is_eval:
            self.eval()
        else:
            self.train()

            # Gaussian noise injection
            if self.weight_noise_injection:
                self._inject_weight_noise(mean=0., std=self.weight_noise_std)

        # Encode acoustic features
        logits, perm_indices = self._encode(
            inputs_var, inputs_seq_len_var, volatile=is_eval)

        # Permutate indices
        if perm_indices is not None:
            labels_var = labels_var[perm_indices.cpu()]
            inputs_seq_len_var = inputs_seq_len_var[perm_indices]
            labels_seq_len_var = labels_seq_len_var[perm_indices.cpu()]

        # Concatenate all labels for warpctc_pytorch
        # `[B, T_out]` -> `[1,]`
        concatenated_labels = _concatenate_labels(
            labels_var, labels_seq_len_var)

        # Output smoothing
        if self.logits_temperature != 1:
            logits /= self.logits_temperature

        # Modify inputs_seq_len_var for reducing time resolution
        if self.encoder.conv is not None or self.encoder_type == 'cnn':
            for i in range(len(inputs_seq_len_var)):
                inputs_seq_len_var.data[i] = self.encoder.conv_out_size(
                    inputs_seq_len_var.data[i], 1)
        inputs_seq_len_var /= 2 ** sum(self.subsample_list)
        # NOTE: floor is not needed because inputs_seq_len_var is IntTensor

        # Compute CTC loss
        ctc_loss_fn = CTCLoss()
        ctc_loss = ctc_loss_fn(logits, concatenated_labels,
                               inputs_seq_len_var.cpu(), labels_seq_len_var)

        # Average the loss by mini-batch
        batch_size = logits.size(1)
        ctc_loss /= batch_size

        return ctc_loss

    def _encode(self, inputs, inputs_seq_len, volatile, is_multi_task=False):
        """Encode acoustic features.
        Args:
            inputs (FloatTensor): A tensor of size `[B, T, input_size]`
            inputs_seq_len (IntTensor): A tensor of size `[B]`
            volatile (bool): if True, the history will not be saved.
                This should be used in inference model for memory efficiency.
            is_multi_task (bool, optional):
        Returns:
            logits (FloatTensor): A tensor of size
                `[T, B, num_classes (including blank)]`
            logits_sub (FloatTensor): A tensor of size
                `[T, B, num_classes_sub (including blank)]`
            perm_indices (LongTensor):
        """
        if is_multi_task:
            encoder_outputs, _, encoder_outputs_sub, _, perm_indices = self.encoder(
                inputs, inputs_seq_len, volatile, pack_sequence=True)
        else:
            if self.encoder_type != 'cnn':
                encoder_outputs, _, perm_indices = self.encoder(
                    inputs, inputs_seq_len, volatile, pack_sequence=True)
            else:
                encoder_outputs = self.encoder(inputs)
                # NOTE: `[B, T, feature_dim]`
                encoder_outputs = encoder_outputs.transpose(0, 1).contiguous()
                perm_indices = None

        if len(self.fc_list) > 0:
            encoder_outputs = self.fc_layers(encoder_outputs)
        logits = self.fc(encoder_outputs)

        if is_multi_task:
            logits_sub = self.fc_sub(encoder_outputs_sub)
            return logits, logits_sub, perm_indices
        else:
            return logits, perm_indices

    def posteriors(self, inputs, inputs_seq_len, temperature=1,
                   blank_prior=None, is_sub_task=False):
        """Returns CTC posteriors (after the softmax layer).
        Args:
            inputs (np.ndarray): A tensor of size `[B, T_in, input_size]`
            inputs_seq_len (np.ndarray): A tensor of size `[B]`
            temperature (float, optional): the temperature parameter for the
                softmax layer in the inference stage
            blank_prior (float, optional):
            is_sub_task (bool, optional):
        Returns:
            probs (np.ndarray): A tensor of size `[B, T, num_classes]`
        """
        # Wrap by Variable
        inputs_var = np2var(inputs, use_cuda=self.use_cuda, volatile=True)
        inputs_seq_len_var = np2var(
            inputs_seq_len, dtype='int', use_cuda=self.use_cuda, volatile=True)

        # Change to evaluation mode
        self.eval()

        # Encode acoustic features
        if hasattr(self, 'main_loss_weight'):
            if is_sub_task:
                _, logits, perm_indices = self._encode(
                    inputs_var, inputs_seq_len_var, volatile=True, is_multi_task=True)
            else:
                logits, _, perm_indices = self._encode(
                    inputs_var, inputs_seq_len_var, volatile=True, is_multi_task=True)
        else:
            logits, perm_indices = self._encode(
                inputs_var, inputs_seq_len_var, volatile=True)

        # Convert to batch-major
        logits = logits.transpose(0, 1)

        probs = F.softmax(logits / temperature, dim=-1)

        # Divide by blank prior
        if blank_prior is not None:
            raise NotImplementedError

        # Permutate indices to the original order
        if perm_indices is not None:
            probs = probs[perm_indices]

        return var2np(probs)

    def decode(self, inputs, inputs_seq_len, beam_width=1,
               max_decode_length=None, is_sub_task=False):
        """
        Args:
            inputs (np.ndarray): A tensor of size `[B, T_in, input_size]`
            inputs_seq_len (np.ndarray): A tensor of size `[B]`
            beam_width (int, optional): the size of beam
            max_decode_length: not used (to match interface)
            is_sub_task (bool, optional):
        Returns:
            best_hyps (np.ndarray):
        """
        # Wrap by Variable
        inputs_var = np2var(inputs, use_cuda=self.use_cuda, volatile=True)
        inputs_seq_len_var = np2var(
            inputs_seq_len, dtype='int', use_cuda=self.use_cuda, volatile=True)

        # Change to evaluation mode
        self.eval()

        # Encode acoustic features
        if hasattr(self, 'main_loss_weight'):
            if is_sub_task:
                _, logits, perm_indices = self._encode(
                    inputs_var, inputs_seq_len_var,
                    volatile=True, is_multi_task=True)
            else:
                logits, _, perm_indices = self._encode(
                    inputs_var, inputs_seq_len_var,
                    volatile=True, is_multi_task=True)
        else:
            logits, perm_indices = self._encode(
                inputs_var, inputs_seq_len_var, volatile=True)

        # Permutate indices
        if perm_indices is not None:
            perm_indices = var2np(perm_indices)
            inputs_seq_len_var = inputs_seq_len_var[perm_indices]

        # Convert to batch-major
        logits = logits.transpose(0, 1)

        # Modify inputs_seq_len_var for reducing time resolution
        if self.encoder.conv is not None or self.encoder_type == 'cnn':
            for i in range(len(inputs_seq_len_var)):
                inputs_seq_len_var.data[i] = self.encoder.conv_out_size(
                    inputs_seq_len_var.data[i], 1)
        if is_sub_task:
            inputs_seq_len_var /= 2 ** sum(
                self.subsample_list[:self.num_layers_sub])
        else:
            inputs_seq_len_var /= 2 ** sum(self.subsample_list)
        # NOTE: floor is not needed because inputs_seq_len_var is IntTensor

        log_probs = F.log_softmax(logits, dim=-1)

        if beam_width == 1:
            best_hyps = self._decode_greedy_np(
                var2np(log_probs), var2np(inputs_seq_len_var))
        else:
            best_hyps = self._decode_beam_np(
                var2np(log_probs), var2np(inputs_seq_len_var),
                beam_width=beam_width)

        # NOTE: index 0 is reserved for blank in warpctc_pytorch
        best_hyps = best_hyps - 1

        # Permutate indices to the original order
        if perm_indices is not None:
            best_hyps = best_hyps[perm_indices]

        return best_hyps

    def decode_from_probs(self, probs, inputs_seq_len, beam_width=1,
                          max_decode_length=None):
        """
        Args:
            probs (np.ndarray):
            inputs_seq_len (np.ndarray):
            beam_width (int, optional):
            max_decode_length (int, optional):
        Returns:
            best_hyps (np.ndarray):
        """
        # TODO: Subsampling

        # Convert to log-scale
        log_probs = np.log(probs + 1e-10)

        if beam_width == 1:
            best_hyps = self._decode_greedy_np(log_probs, inputs_seq_len)
        else:
            best_hyps = self._decode_beam_np(
                log_probs, inputs_seq_len, beam_width=beam_width)

        # NOTE: index 0 is reserved for blank in warpctc_pytorch
        best_hyps = best_hyps - 1

        return best_hyps


def _concatenate_labels(labels, labels_seq_len):
    """Concatenate all labels in mini-batch and convert to a 1D tensor.
    Args:
        labels (LongTensor): A tensor of size `[B, T_out]`
        labels_seq_len (IntTensor): A tensor of size `[B]`
    Returns:
        concatenated_labels (): A tensor of size `[all_label_num]`
    """
    batch_size = labels.size(0)
    total_lables_seq_len = labels_seq_len.data.sum()
    concatenated_labels = Variable(
        torch.zeros(total_lables_seq_len)).int()
    label_counter = 0
    for i_batch in range(batch_size):
        concatenated_labels[label_counter:label_counter +
                            labels_seq_len.data[i_batch]] = labels[i_batch][:labels_seq_len.data[i_batch]]
        label_counter += labels_seq_len.data[i_batch]

    return concatenated_labels
