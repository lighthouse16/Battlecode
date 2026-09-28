"""Baseline Random Bot."""
from battlelab.adapters.mock.bots import policy_random

def run(state):
    return policy_random(state)
