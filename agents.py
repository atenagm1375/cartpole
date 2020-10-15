"""
==============================================================================.

agents.py

@author: atenagm

==============================================================================.
"""
import torch

from abc import ABC, abstractmethod

from bindsnet.environment import GymEnvironment
from bindsnet.network import Network
from bindsnet.learning.reward import AbstractReward
from bindsnet.network.nodes import Input, DiehlAndCookNodes
from bindsnet.network.topology import Connection
from bindsnet.learning import WeightDependentPostPre, MSTDPET
from bindsnet.network.monitors import Monitor


class Agent(ABC):
    """
    Abstract base class for agents.

    Parameters
    ----------
    environment : GymEnvironment
        The environment of the agent.
    allow_gpu : bool, optional
        Allows automatic transfer to the GPU. The default is True.

    """

    @abstractmethod
    def __init__(
            self,
            environment: GymEnvironment,
            allow_gpu: bool = True,
            ) -> None:

        super().__init__()

        self.environment = environment

        if allow_gpu and torch.cuda.is_available():
            self.allow_gpu = True
            self.device = torch.device("cuda")
        else:
            self.allow_gpu = False
            self.device = torch.device("cpu")

    @abstractmethod
    def select_action(self,
                      **kwargs) -> int:
        """
        Abstract method to select an action.

        Keyword Arguments
        -----------------

        Returns
        -------
        action : int
            The action to be taken.

        """
        action = -1
        return action


class ObserverAgent(Agent):
    """
    Observer agent in Gym environment.

    Parameters
    ----------
    environment : GymEnvironment
        The environment of the observer agent.
    method : str, optional
        The method that the agent acts upon. Possible values are:
            `first_spike`: Select the action with the first spike.
            `softmax`: Select an action using softmax function on the spikes.
            `random`: Select actions randomly.
        The default is 'fist_spike'.
    dt : float, optional
        Network simulation timestep. The default is 1.0.
    learning : bool, optional
        Whether to allow network connection updates. The default is True.
    reward_fn : AbstractReward, optional
        Optional class allowing for modification of reward in case of
        reward-modulated learning. The default is None.
    allow_gpu : bool, optional
        Allows automatic transfer to the GPU. The default is True.

    """

    def __init__(
            self,
            environment: GymEnvironment,
            method: str = 'first_spike',
            dt: float = 1.0,
            learning: bool = True,
            reward_fn: AbstractReward = None,
            allow_gpu: bool = True,
            ) -> None:

        super().__init__(environment, allow_gpu)

        self.method = method

        input_shape = self.environment.env.observation_space.shape
        output_shape = self.environment.env.action_space.shape

        self.network = Network(dt=dt, learning=learning, reward_fn=reward_fn)

        # TODO Consider network structure
        s2 = Input(shape=[1, *input_shape, 10], traces=True)
        pfc = Input(n=1000, traces=True)
        sts = DiehlAndCookNodes(n=500, traces=True,
                                thresh=-52.0,
                                rest=-65.0,
                                reset=-65.0,
                                refrac=5,
                                tc_decay=100.0,
                                theta_plus=0.05,
                                tc_theta_decay=1e7)
        pm = DiehlAndCookNodes(shape=[*output_shape, 20], traces=True,
                               thresh=-52.0,
                               rest=-65.0,
                               reset=-65.0,
                               refrac=5,
                               tc_decay=100.0,
                               theta_plus=0.05,
                               tc_theta_decay=1e7)

        s2_sts = Connection(s2, sts,
                            nu=[0.05, 0.04],
                            update_rule=WeightDependentPostPre,
                            wmin=0.0,
                            wmax=0.2)
        sts_pm = Connection(sts, pm,
                            nu=[0.05, 0.04],
                            update_rule=MSTDPET,
                            wmin=0.0,
                            wmax=1.0,
                            norm=0.5 * sts.n)
        pfc_pm = Connection(pfc, pm,
                            nu=[0.05, 0.04],
                            update_rule=MSTDPET,
                            wmin=0.0,
                            wmax=1.0,
                            norm=0.25 * pfc.n)
        pm_pm = Connection(pm, pm,
                           nu=[0.05, 0.04],
                           wmin=-0.1,
                           wmax=0.)

        self.network.add_layer(s2, "S2")
        self.network.add_layer(sts, "STS")
        self.network.add_layer(pfc, "PFC")
        self.network.add_layer(pm, "PM")

        self.network.add_connection(s2_sts, "S2", "STS")
        self.network.add_connection(sts_pm, "STS", "PM")
        self.network.add_connection(pfc_pm, "PFC", "PM")
        self.network.add_connection(pm_pm, "PM", "PM")

        self.network.add_monitor(
            Monitor(self.network.layers["PM"], ["s"]),
            "PM",
        )

        self.network.to(self.device)

    def select_action(self,
                      **kwargs) -> int:
        """
        Choose the proper action based on observation.

        Keyword Arguments
        -----------------

        Returns
        -------
        action : int
            The action to be taken.

        """
        spikes = (self.network.monitors["PM"].get("s").float())

        # Select action based on first spike.
        if self.method == 'first_spike':
            spikes = spikes.squeeze().squeeze().nonzero()

            if spikes.shape[0] == 0:
                return self.environment.action_space.sample()
            else:
                return spikes[0, 1]

        # Select action using softmax.
        if self.method == 'softmax':
            spikes = torch.sum(spikes, dim=0)
            probs = torch.softmax(spikes, dim=0)
            return torch.multinomial(probs, num_samples=1).item()

        # Select action randomly.
        if self.method == 'random' or self.method is None:
            return self.environment.action_space.sample()


class ExpertAgent(Agent):
    """
    Expert agent in Gym environment.

    Parameters
    ----------
    environment : GymEnvironment
        Environment of the expert agent.
    method : str, optional
        Defines the method that agent acts upon. Possible values:
            `random`: Expert acts randomly.
            `manual`: Expert action is controlled by some human user.
            `from_weight`: Expert acts based on some weight matrix from a
                           trained network.
            `user-defined`: Expert is controlled by a user-defined function.
        The default is 'random'.
    allow_gpu : bool, optional
        Allows automatic transfer to the GPU. The default is True.

    """

    def __init__(self,
                 environment: GymEnvironment,
                 method: str = 'random',
                 allow_gpu: bool = True,
                 ) -> None:

        super().__init__(environment, allow_gpu)
        self.method = method

    def select_action(self,
                      **kwargs) -> int:
        """
        Choose the proper action based on observation.

        Keyword Arguments
        -----------------
        weight : str or torch.Tensor
            The weight matrix from a trained network. It contains one of the:
                1) String of the path to weight file.
                2) The weight tensor itself.
            Used when method is set to `from_weight`.
        function : callable
            The control function define by user.
            Used when method is set to `user-defined`.

        Returns
        -------
        action : int
            The action to be taken.

        """
        # Expert acts randomly
        if self.method == 'random' or self.method is None:
            return self.environment.action_space.sample()

        # Expert is controlled manually by a human
        if self.method == 'manual':
            # TODO implement arrow key control
            return

        state = torch.from_numpy(self.environment.env.state)

        # Expert acts based on the weight matrix of a trained network.
        if self.method == 'from_weight':
            weight = kwargs['weight']
            if isinstance(weight, str):
                weight = torch.load(weight)

            return torch.argmax(torch.matmul(state, weight))

        # Expert is controlled by some user-defined control function.
        if self.method == 'user-defined':
            return kwargs['function'](state, **kwargs)

        raise ValueError
