"""Baseline Fixed Bot."""
from battlelab.adapters.mock.bots import policy_fixed

def run(state):
    return policy_fixed(state)
