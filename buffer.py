import operator
import random
from typing import List
import numpy as np
import torch
import math

import config

obs_pad = np.zeros((config.obs_dim, 9, 9), dtype=config.dtype)
pos_pad = np.zeros((config.pos_dim), dtype=config.dtype)

discounts = np.array([[0.99**i] for i in range(config.max_steps)])

class SumTree:
    def __init__(self, capacity, priorities=None):

        layer = 1
        while 2**(layer-1) < capacity:
            layer += 1
        assert 2**(layer-1) == capacity, 'buffer size only support power of 2 size'
        self.layer = layer
        self.tree = np.zeros(2**layer-1)
        self.capacity = capacity
        if priorities is not None:
            self.size = len(priorities)
        else:
            self.size = 0

        if priorities is not None:
            idx = np.array([ self.capacity-1+i for i in range(len(priorities)) ], dtype=np.int)
            self.tree[idx] = priorities

            for _ in range(layer-1):
                idx = (idx-1) // 2
                idx = np.unique(idx)
                self.tree[idx] = self.tree[2*idx+1] + self.tree[2*idx+2]
            
            # check
            self.sum()


    def sum(self):
        assert int(np.sum(self.tree[-self.capacity:])) == int(self.tree[0]), 'sum is {} but root is {}'.format(np.sum(self.tree[-self.capacity:]), self.tree[0])
        return self.tree[0]

    def __getitem__(self, idx:int):
        assert 0 <= idx < self.capacity

        return self.tree[self.capacity-1+idx]

    def find_prefixsum_idx(self, prefixsum:float):
        """
        Find the highest index `i` in the array such that
            sum(arr[0] + arr[1] + ... + arr[i]) <= prefixsum
        """
        
        assert 0 <= prefixsum <= self.sum() + 1e-5

        idx = 0
        while idx < self.capacity-1:  # while non-leaf
            if self.tree[2*idx + 1] > prefixsum:
                idx = 2*idx + 1
            else:
                prefixsum -= self.tree[2*idx + 1]
                idx = 2*idx + 2

        return idx-self.capacity+1, prefixsum

    def update(self, idx:int, priority:float):
        assert 0 <= idx < self.capacity
        # self.tree.flags.writeable = True

        idx += self.capacity-1

        self.tree[idx] = priority

        idx = (idx-1) // 2
        while idx >= 0:
            self.tree[idx] = self.tree[2*idx+1] + self.tree[2*idx+2]
            idx = (idx-1) // 2
        
        assert int(np.sum(self.tree[-self.capacity:])) == int(self.tree[0]), 'sum is {} but root is {}'.format(np.sum(self.tree[-self.capacity:]), self.tree[0])
        
    def batch_update(self, idxes:np.ndarray, priorities:np.ndarray):
        idxes += self.capacity-1
        self.tree[idxes] = priorities

        for _ in range(self.layer-1):
            idxes = (idxes-1) // 2
            idxes = np.unique(idxes)
            self.tree[idxes] = self.tree[2*idxes+1] + self.tree[2*idxes+2]
        
        # check
        assert int(np.sum(self.tree[-self.capacity:])) == int(self.tree[0]), 'sum is {} but root is {}'.format(np.sum(self.tree[-self.capacity:]), self.tree[0])


class LocalBuffer:
    def __init__(self, init_obs_pos, imitation:bool, size=config.max_steps, alpha=config.prioritized_replay_alpha, beta=config.prioritized_replay_beta):
        """
        Prioritized Replay buffer for each actor

        """
        assert alpha >= 0
        self.alpha = alpha
        self.beta = beta

        self.num_agents = init_obs_pos[0].shape[0]
        # observation length should be (max steps+1)
        self.obs_buf = np.zeros((size+1, self.num_agents, *config.obs_shape), dtype=np.bool)
        self.pos_buf = np.zeros((size+1, self.num_agents, 4), dtype=np.uint8)
        self.act_buf = np.zeros((size, self.num_agents), dtype=np.uint8)
        self.rew_buf = np.zeros((size, self.num_agents), dtype=np.float32)
        self.q_buf = np.zeros((size+1, self.num_agents, 5), dtype=np.float32)

        self.capacity = size
        self.size = 0
        self.imitation = imitation

        self.obs_buf[0], self.pos_buf[0] = init_obs_pos
    
    def __len__(self):
        return self.size

    def add(self, q_val:np.ndarray, actions:List[int], reward:List[float], next_obs_pos:np.ndarray):

        assert self.size < self.capacity

        self.act_buf[self.size] = actions
        self.rew_buf[self.size] = reward
        self.obs_buf[self.size+1], self.pos_buf[self.size+1] = next_obs_pos
        self.q_buf[self.size] = q_val

        self.size += 1

    def finish(self, last_q_val=None):
        # last q value is None if done
        if last_q_val is None:
            self.done = True
        else:
            self.done = False
            self.q_buf[self.size] = last_q_val
        
        self.obs_buf = self.obs_buf[:self.size+1]
        self.pos_buf = self.pos_buf[:self.size+1]
        self.act_buf = self.act_buf[:self.size]
        self.rew_buf = self.rew_buf[:self.size]
        self.q_buf = self.q_buf[:self.size+1]


        if self.imitation:
            assert self.done, 'size {}'.format(self.size)

        priorities = np.zeros((self.size, self.num_agents), dtype=np.float32)

        if self.imitation:
            for i in range(self.size):
                reward = np.sum(self.rew_buf[i:self.size]*discounts[:self.size-i], axis=0)
                q_val = np.max(self.q_buf[i], axis=1)
                priorities[i] = np.abs(reward-q_val)
            priorities = np.mean(priorities, axis=1)

        else:
            for i in range(self.size):
                reward = self.rew_buf[i]+0.99*np.max(self.q_buf[i+1], axis=1)
                q_val = np.max(self.q_buf[i], axis=1)
                priorities[i] = np.abs(reward-q_val)
            priorities = np.mean(priorities, axis=1)

        self.priority_tree = SumTree(self.capacity, priorities)
        self.priority = self.priority_tree.sum()
        
    def sample(self, prefixsum):

        idx, _ = self.priority_tree.find_prefixsum_idx(prefixsum)

        assert 0 <= idx < self.size

        priority = self.priority_tree[idx]

        encoded_sample = self._encode_sample(idx)

        return encoded_sample + (idx, priority)

    def update_priority(self, idx, priority):
        assert 0 <= idx < self.size, 'idx {} out of size {}'.format(idx, self.size)

        self.priority_tree.update(idx, priority**self.alpha)
        self.priority = self.priority_tree.sum()


    def _encode_sample(self, idx):

        if self.imitation:
            # use Monte Carlo method if it's imitation
            forward = self.size - idx
            reward = np.sum(self.rew_buf[idx:self.size]*discounts[:self.size-idx], axis=0)
        else:
            # self play
            forward = 1
            reward = self.rew_buf[idx]

        # obs and pos
        bt_steps = min(idx+1, config.bt_steps)
        obs = np.swapaxes(self.obs_buf[idx+1-bt_steps:idx+1], 0, 1)
        obs = obs.reshape(self.num_agents*bt_steps, *config.obs_shape)

        pos = np.swapaxes(self.pos_buf[idx+1-bt_steps:idx+1], 0, 1)
        pos = pos.reshape(self.num_agents*bt_steps, 4)

        # next obs and next pos
        next_bt_steps = min(idx+2, config.bt_steps)
        next_obs = np.swapaxes(self.obs_buf[idx+2-next_bt_steps:idx+2], 0, 1)
        next_obs = next_obs.reshape(self.num_agents*next_bt_steps, *config.obs_shape)

        next_pos = np.swapaxes(self.pos_buf[idx+2-next_bt_steps:idx+2], 0, 1)
        next_pos = next_pos.reshape(self.num_agents*next_bt_steps, 4)

        # define other part
        done = [ self.done for _ in range(self.num_agents) ]
        steps = [ forward for _ in range(self.num_agents) ]
        bt_steps = [ bt_steps for _ in range(self.num_agents) ]
        next_bt_steps = [ next_bt_steps for _ in range(self.num_agents) ]

        return obs, pos, self.act_buf[idx].tolist(), reward.tolist(), next_obs, next_pos, done, steps, bt_steps, next_bt_steps



