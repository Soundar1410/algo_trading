"""Shared platform code.

Dependency direction is one-way (spec section 5): strategies and runtimes import
from ``common``; ``common`` never imports a strategy or a runtime.
"""
