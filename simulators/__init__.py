"""Protocol simulators for development and tests. NEVER imported by drivers or production code.

They implement the server side of each protocol independently from the client libraries, so
simulation tests exercise real framing on real sockets — but they do not prove compatibility
with physical PLCs (see docs/COMPATIBILITY.md).
"""
