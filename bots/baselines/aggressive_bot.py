"""Baseline Aggressive Bot."""
from battlelab.adapters.mock.bots import policy_aggressive

def run(state):
    return policy_aggressive(state)
