"""The render engine: `ordo.yaml` + the model catalog + the service manifests -> `out/`.

Also the contracts of what it renders, which the control plane and the host share: the compose
file and the one `docker compose` argv builder (stack.py), the first-party image record
(image_tags.py) and the models volume (models_volume.py). Imports nothing from ordo.control or
ordo.host. The render entry point is ordo/render/engine.py.
"""
