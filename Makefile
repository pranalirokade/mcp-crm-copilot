.PHONY: install seed triage test mcp
install:
	pip install -r requirements.txt
seed:
	PYTHONPATH=src python -m copilot.cli seed
triage: seed
	PYTHONPATH=src python -m copilot.cli triage
test:
	PYTHONPATH=src pytest -q
mcp:
	PYTHONPATH=src python -m copilot.mcp_server
