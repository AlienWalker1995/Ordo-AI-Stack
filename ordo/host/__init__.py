"""The operator's host commands: install, bring-up, deploy, build, fetch, secrets, remote access,
preflight and doctor (their argparse handlers are the cli_*.py modules). Imports ordo.render only;
it reaches the control plane over HTTP (ops-controller's /status), never by import.
"""
