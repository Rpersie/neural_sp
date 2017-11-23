#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""Base class for loading dataset for the CTC and attention-based model.
   In this class, all data will be loaded at each step.
   You can use the multi-GPU version.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from os.path import basename, isfile
import math
import random
import numpy as np

from utils.dataset.base import Base
from utils.io.inputs.frame_stacking import stack_frame
from utils.io.inputs.splicing import do_splice
from utils.io.variable import np2var
# from utils.parallel import make_parallel

# NOTE: Loading numpy is faster than loading htk


class DatasetBase(Base):

    def __init__(self, *args, **kwargs):
        super(DatasetBase, self).__init__(*args, **kwargs)

    def __next__(self, batch_size=None):
        """Generate each mini-batch.
        Args:
            batch_size (int, optional): the size of mini-batch
        Returns:
            A tuple of `(inputs, labels, inputs_seq_len, labels_seq_len, input_names)`
                inputs: list of input data of size
                    `[num_gpus, B, T_in, input_size]`
                labels: list of target labels of size
                    `[num_gpus, B, T_out]`
                inputs_seq_len: list of length of inputs of size
                    `[num_gpus, B]`
                labels_seq_len: list of length of target labels of size
                    `[num_gpus, B]`
                input_names: list of file name of input data of size
                    `[num_gpus, B]`
            is_new_epoch (bool): If true, 1 epoch is finished
        """
        if self.max_epoch is not None and self.epoch >= self.max_epoch:
            raise StopIteration
        # NOTE: max_epoch = None means infinite loop

        if batch_size is None:
            batch_size = self.batch_size

        # reset
        if self.is_new_epoch:
            self.is_new_epoch = False

        if self.sort_utt or not self.shuffle:
            if len(self.rest) > batch_size:
                data_indices = self.df[batch_size *
                                       self.offset:batch_size * (self.offset + 1)].index
                data_indices = list(data_indices)
                self.rest -= set(list(data_indices))
                # NOTE: rest is in uttrance length order when sort_utt == True
                # NOTE: otherwise in name length order when shuffle == False
            else:
                # Last mini-batch
                data_indices = list(self.rest)
                self.reset()
                self.is_new_epoch = True
                self.epoch += 1
                if self.epoch == self.sort_stop_epoch:
                    self.sort_utt = False
                    self.shuffle = True

            # Shuffle data in the mini-batch
            random.shuffle(data_indices)
        else:
            # Randomly sample uttrances
            if len(self.rest) > batch_size:
                data_indices = random.sample(list(self.rest), batch_size)
                self.rest -= set(data_indices)
            else:
                # Last mini-batch
                data_indices = list(self.rest)
                self.reset()
                self.is_new_epoch = True
                self.epoch += 1

                # Shuffle selected mini-batch
                random.shuffle(data_indices)

        # Tokenize
        if self.epoch == 0 or (self.epoch == 1 and self.is_new_epoch):
            for i in data_indices:
                indices = self.map_fn(self.df['transcript'][i])
                str_indices = ' '.join(list(map(str, indices.tolist())))
                self.df['index'][i] = str_indices

                # Change path
                new_input_path = self.df['input_path'][i].replace(
                    '/n/sd8/inaguma/', '/data/inaguma/')
                if isfile(new_input_path):
                    self.df['input_path'][i] = new_input_path

        # Load dataset in mini-batch
        input_path_list = np.array(self.df['input_path'][data_indices])
        str_indices_list = np.array(self.df['index'][data_indices])

        if not hasattr(self, 'input_size'):
            if self.save_format == 'numpy':
                self.input_size = self.load_npy(input_path_list[0]).shape[-1]
            elif self.save_format == 'htk':
                self.input_size = self.read_htk(input_path_list[0]).shape[-1]
            else:
                raise TypeError
            # self.input_size = (input_data_list[0]).shape[-1]
            self.input_size *= self.num_stack
            self.input_size *= self.splice

        # Compute max frame num in mini-batch
        max_frame_num = max(self.df['frame_num'][data_indices])
        max_frame_num = math.ceil(max_frame_num / self.num_skip)

        # Compute max target label length in mini-batch
        max_seq_len = max(
            map(lambda x: len(x.split(' ')), str_indices_list)) + 2
        # NOTE: add <SOS> and <EOS>

        # Initialization
        inputs = np.zeros(
            (len(data_indices), max_frame_num, self.input_size),
            dtype=np.float32)
        labels = np.array(
            [[self.att_padded_value] * max_seq_len] * len(data_indices))
        inputs_seq_len = np.zeros((len(data_indices),), dtype=np.int32)
        labels_seq_len = np.zeros((len(data_indices),), dtype=np.int32)
        input_names = np.array(list(
            map(lambda path: basename(path).split('.')[0],
                np.array(self.df['input_path'][data_indices]))))

        # Set values of each data in mini-batch
        for i_batch in range(len(data_indices)):
            # Load input data
            if self.save_format == 'numpy':
                data_i = self.load_npy(input_path_list[i_batch])
            elif self.save_format == 'htk':
                data_i = self.read_htk(input_path_list[i_batch])
            else:
                raise TypeError

            # Frame stacking
            data_i = stack_frame(data_i, self.num_stack, self.num_skip)
            frame_num = data_i.shape[0]

            # Splicing
            data_i = do_splice(data_i, self.splice, self.num_stack)

            inputs[i_batch, : frame_num, :] = data_i
            inputs_seq_len[i_batch] = frame_num
            indices = list(map(int, str_indices_list[i_batch].split(' ')))
            label_num = len(indices)

            if self.is_test:
                labels[i_batch, 0] = self.df['transcript'][data_indices[i_batch]]
            else:
                if self.model_type == 'attention':
                    labels[i_batch, 0] = self.sos_index
                    labels[i_batch, 1:label_num + 1] = indices
                    labels[i_batch, label_num + 1] = self.eos_index
                    labels_seq_len[i_batch] = label_num + 2
                    # NOTE: include <SOS> and <EOS>
                elif self.model_type == 'ctc':
                    labels[i_batch, 0:label_num] = indices
                    labels_seq_len[i_batch] = label_num
                else:
                    raise TypeError

        # Now we split the mini-batch data by num_gpus
        # inputs = self.split_per_device(inputs, self.num_gpus)
        # labels = self.split_per_device(labels, self.num_gpus)
        # inputs_seq_len = self.split_per_device(inputs_seq_len, self.num_gpus)
        # labels_seq_len = self.split_per_device(labels_seq_len, self.num_gpus)
        # input_names = self.split_per_device(input_names, self.num_gpus)

        # Wrap by variable
        inputs = np2var(inputs, use_cuda=self.use_cuda, volatile=self.volatile)
        inputs_seq_len = np2var(
            inputs_seq_len, use_cuda=self.use_cuda, volatile=self.volatile, dtype='int')

        if not self.is_test:
            labels = np2var(
                labels, use_cuda=self.use_cuda, volatile=self.volatile, dtype='long')
            labels_seq_len = np2var(
                labels_seq_len, use_cuda=self.use_cuda, volatile=self.volatile, dtype='int')

        self.iteration += len(data_indices)
        self.offset += len(data_indices)

        return (inputs, labels, inputs_seq_len, labels_seq_len,
                input_names), self.is_new_epoch