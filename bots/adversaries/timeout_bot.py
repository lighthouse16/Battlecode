"""Adversarial Timeout Test Bot."""
from battlelab.adapters.mock.bots import policy_timeout

def run(state):
    return policy_timeout(state)
