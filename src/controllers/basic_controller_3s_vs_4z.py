from modules.agents import REGISTRY as agent_REGISTRY
from components.action_selectors import REGISTRY as action_REGISTRY
import torch as th
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

class RNN_msg(nn.Module):
    def __init__(self, input_shape, hidden_shape, action_shape):
        super(RNN_msg, self).__init__()
        self.hidden_shape = hidden_shape
        self.fc1 = nn.Linear(input_shape, hidden_shape)
        self.rnn = nn.GRUCell(hidden_shape, action_shape)

    def init_hidden(self):
        # make hidden states on same device as model
        return self.fc1.weight.new(1, self.hidden_shape).zero_()

    def forward(self, hidden_state, input_h):
        x = F.elu(self.fc1(input_h))
        h_in = hidden_state.reshape(-1, self.hidden_shape)
        h = self.rnn(x, h_in)
        return h, h


# This multi-agent controller shares parameters between agents
class BasicMAC_3s_vs_4z:
    def __init__(self, scheme, groups, args):
        self.n_agents = args.n_agents
        self.args = args
        input_shape = self._get_input_shape(scheme)
        self._build_agents(input_shape)
        self.agent_output_type = args.agent_output_type

        self.action_selector = action_REGISTRY[args.action_selector](args)
        self.match_weight = 0.0;
        self.msg_rnn = RNN_msg(64,10,10).cuda()
        self.epsilon = 1e-10
        self.hidden_states = None
        self.transmit_gap = th.zeros((24)).cuda()       # time gap since last transmit, for sender to check timeout 
        self.receive_gap = th.zeros((8,3,3)).cuda()     # time gap since last receive, for receiver to check timeout
        self.msg_old_test = th.zeros((24,10)).cuda()    # sender buffer
        self.msg_old_test_reshape = th.zeros((8,3,3,10)).cuda()    
        
        ##################hyperparameters######################
        self.fresh_limit = self.args.fresh_limit*2    # validation period for the messages in the received message buffer
        self.transmit_limit = self.args.transmit_limit   # if the transmitter has not sent within this limit, the transmitter will send
        self.delta = self.args.delta    # delta in Algorithm 1
        self.average_transmission = 0.0
        self.number_episode = 0
        #######################################################

    def select_actions(self, ep_batch, t_ep, t_env, bs=slice(None), test_mode=False):
        # Only select actions for the selected batch elements in bs
        avail_actions = ep_batch["avail_actions"][:, t_ep]
        agent_local_outputs, input_hidden_states, vi = self.forward(ep_batch, t_ep, test_mode=test_mode)
        # store all the actions
        input_hidden_states = input_hidden_states.view(-1,64)
        self.hidden_states_msg, dummy = self.msg_rnn(self.e, input_hidden_states)
        dummy = dummy.reshape(8,3,10)
        dummy0 = dummy[:,0,:]
        dummy1 = dummy[:,1,:]
        dummy2 = dummy[:,2,:]       
        agent0 = (dummy1 + dummy2)/2.0
        agent1 = (dummy0 + dummy2)/2.0
        agent2 = (dummy0 + dummy1)/2.0
        agent_global_outputs =th.cat((agent0.view((8,1,10)),agent1.view((8,1,10)),agent2.view((8,1,10))),1)
        
        agent_outputs = agent_local_outputs + agent_global_outputs
        chosen_actions = self.action_selector.select_action(agent_outputs[bs], avail_actions[bs], t_env, test_mode=test_mode)

        return chosen_actions
    # 该方法用于在一个episode中每个时刻为所有智能体选择动作。
    # t_ep代表当前样本在一个episode中的时间索引。t_env代表当前时刻环境运行的总时间，用于计算epsilon-greedy中的epsilon。
    def select_actions_noisy_env(self, loss_pattern, ep_batch, t_ep, t_env, bs=slice(None), test_mode=False):
        # Only select actions for the selected batch elements in bs
        avail_actions = ep_batch["avail_actions"][:, t_ep]
        # 获得episode内每个时刻的观测对应的所有动作的Q值与隐层变量mac.hidden_states
        agent_local_outputs, input_hidden_states, vi = self.forward(ep_batch, t_ep, test_mode=test_mode)
        
        # store all the actions
        input_hidden_states = input_hidden_states.view(-1,64)
        self.hidden_states_msg, dummy = self.msg_rnn(self.hidden_states_msg, input_hidden_states)
        ###################send message to teammates#############################
        dd = (((dummy-self.msg_old_test)**2).sum(1) > self.delta).float()  
        self.transmit_gap[dd==0] = self.transmit_gap[dd==0] + 1
        dd[self.transmit_gap>=self.transmit_limit] = 1
        self.transmit_gap[dd==1] = 0
        self.msg_old_test[dd==1,:] = dummy[dd==1,:]   # for the messages where dd = 1, transmit it
        #########add loss to the transmitted message################
        dd = dd.reshape(8,3)
        dd = th.stack([dd]*3,2) 
        loss_pattern = loss_pattern.reshape((8,3,3))
        dd = dd * loss_pattern
        ########check invalid messages in the receiver buffer#######
        self.receive_gap[dd==1] = 0
        self.receive_gap[dd==0] = self.receive_gap[dd==0] + 1
        use_old = 1-dd
        use_old[self.receive_gap>self.fresh_limit] = 0
        ########assign the packets to all the receiving agents######
        dd = th.stack([dd]*10,3).float()   
        use_old = th.stack([use_old]*10,3).float()   
        dummy = th.stack([dummy]*3,1).float()  
        dummy = dummy.reshape(8,3,3,10)  
        dummy_final = self.msg_old_test_reshape * use_old + dummy * dd  
        self.msg_old_test_reshape = dummy_final
        #########
        agent0 = (dummy_final[:,1,0,:] + dummy_final[:,2,0,:])/2.0
        agent1 = (dummy_final[:,0,1,:] + dummy_final[:,2,1,:])/2.0
        agent2 = (dummy_final[:,0,2,:] + dummy_final[:,1,2,:])/2.0
        
        agent_global_outputs =th.cat((agent0.view((8,1,10)),agent1.view((8,1,10)),agent2.view((8,1,10))),1)
        
        agent_outputs = agent_local_outputs + agent_global_outputs
        chosen_actions = self.action_selector.select_action(agent_outputs[bs], avail_actions[bs], t_env, test_mode=test_mode)

        return chosen_actions
    
    # ep_batch表示一个episode的样本，t表示每个样本在该episode内的时间索引，
    # forward()方法的作用是输出一个episode内每个时刻的观测对应的所有动作的Q值与隐层变量mac.hidden_states。
    # 由于QMix算法采用的是DRQN网络，因此每个episode的样本必须与mac.hidden_states一起连续输入到神经网络中，
    # 当t变量为0时，即当一个episode第一个时刻的样本输入到神经网络时，mac.hidden_states初始化为0，此后在同一个episode中，mac.hidden_states会持续得到更新。
    
    def forward(self, ep_batch, t, test_mode=False):
        agent_inputs, visibility_matrix = self._build_inputs(ep_batch, t)
        avail_actions = ep_batch["avail_actions"][:, t]
        agent_outs, self.hidden_states = self.agent(agent_inputs, self.hidden_states)    # this is (48,98)!!!!!!!!!
        # Softmax the agent outputs if they're policy logits
        if self.agent_output_type == "pi_logits":
            if getattr(self.args, "mask_before_softmax", True):
                # Make the logits for unavailable actions very negative to minimise their affect on the softmax
                reshaped_avail_actions = avail_actions.reshape(ep_batch.batch_size * self.n_agents, -1)
                agent_outs[reshaped_avail_actions == 0] = -1e10

            agent_outs = th.nn.functional.softmax(agent_outs, dim=-1)
            if not test_mode:
                # Epsilon floor
                epsilon_action_num = agent_outs.size(-1)
                if getattr(self.args, "mask_before_softmax", True):
                    # With probability epsilon, we will pick an available action uniformly
                    epsilon_action_num = reshaped_avail_actions.sum(dim=1, keepdim=True).float()

                agent_outs = ((1 - self.action_selector.epsilon) * agent_outs
                               + th.ones_like(agent_outs) * self.action_selector.epsilon/epsilon_action_num)

                if getattr(self.args, "mask_before_softmax", True):
                    # Zero out the unavailable actions
                    agent_outs[reshaped_avail_actions == 0] = 0.0

        return agent_outs.view(ep_batch.batch_size, self.n_agents, -1), self.hidden_states.view(ep_batch.batch_size, self.n_agents, -1), visibility_matrix

    def init_hidden(self, batch_size):
        self.hidden_states = self.agent.init_hidden().unsqueeze(0).expand(batch_size, self.n_agents, -1)  # bav
        self.hidden_states_msg = self.msg_rnn.init_hidden().unsqueeze(0).expand(batch_size, self.n_agents, -1).cuda()  # bav
        
    def set_match_weight(self, weight):
        self.match_weight = weight

    def parameters(self):
        return self.agent.parameters()

    def load_state(self, other_mac):
        self.msg_rnn.load_state_dict(other_mac.msg_rnn.state_dict())
        self.agent.load_state_dict(other_mac.agent.state_dict())

    def cuda(self):
        self.agent.cuda()
        self.msg_rnn.cuda()
        
    def save_models(self, path):
        th.save(self.msg_rnn.state_dict(), "{}/msg_rnn.th".format(path))
        th.save(self.agent.state_dict(), "{}/agent.th".format(path))

    def load_models(self, path):
        self.msg_rnn.load_state_dict(th.load("{}/msg_rnn.th".format(path), map_location=lambda storage, loc: storage))
        self.agent.load_state_dict(th.load("{}/agent.th".format(path), map_location=lambda storage, loc: storage))

    def _build_agents(self, input_shape):
        self.agent = agent_REGISTRY[self.args.agent](input_shape, self.args)

    def _build_inputs(self, batch, t):
        # Assumes homogenous agents with flat observations.
        # Other MACs might want to e.g. delegate building inputs to each agent
        bs = batch.batch_size
        inputs = []
        inputs.append(batch["obs"][:, t])  # b1av

    
        if self.args.obs_last_action:
            if t == 0:
                inputs.append(th.zeros_like(batch["actions_onehot"][:, t]))
            else:
                inputs.append(batch["actions_onehot"][:, t-1])
                #print(batch["actions_onehot"][:, t-1].shape)  #(8,6,14)
        if self.args.obs_agent_id:
            inputs.append(th.eye(self.n_agents, device=batch.device).unsqueeze(0).expand(bs, -1, -1))
        inputs = th.cat([x.reshape(bs*self.n_agents, -1) for x in inputs], dim=1)  
        visibility_matrix = th.ones((1))
        
        return inputs, visibility_matrix

    def _get_input_shape(self, scheme):
        input_shape = scheme["obs"]["vshape"]
        if self.args.obs_last_action:
            input_shape += scheme["actions_onehot"]["vshape"][0]
        if self.args.obs_agent_id:
            input_shape += self.n_agents

        return input_shape



