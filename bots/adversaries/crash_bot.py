"""Adversarial Crash Test Bot."""
from battlelab.adapters.mock.bots import policy_crash

def run(state):
    return policy_crash(state)
