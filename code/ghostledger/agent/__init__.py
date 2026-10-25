"""Bounded AI investigator: model clients, read-only dataset tools, a tool loop and deterministic validators.

The model never writes output. It investigates through read-only tools and returns structured
proposals; validators.py checks each proposal against the evidence, and only validated facts reach
the deterministic engine (as links in pipeline.Auditor). With no credentials, or on any model
failure, every entry point falls back to the deterministic result.
"""
