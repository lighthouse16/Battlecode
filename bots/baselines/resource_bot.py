"""Baseline Resource Bot."""
from battlelab.adapters.mock.bots import policy_resource_greedy

def run(state):
    return policy_resource_greedy(state)
