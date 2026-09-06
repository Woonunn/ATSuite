from graphlib import TopologicalSorter


def profile_workload(resolved) -> dict:
    """Describe the trace; these values never enter recommendation scoring."""
    nodes = {node.node_id: node for node in resolved.trace.nodes}
    if len(nodes) != len(resolved.trace.nodes) or 0 not in nodes:
        raise ValueError("Trace needs unique node IDs and start node 0")
    parents = {key: set() for key in nodes}
    for node in nodes.values():
        for edge in node.edge_to:
            if edge.target_id not in nodes:
                raise ValueError(f"Unknown trace edge target: {edge.target_id}")
            parents[edge.target_id].add(node.node_id)
    sorter = TopologicalSorter(parents)
    sorter.prepare()
    depth, max_parallelism = 0, 0
    while sorter.is_active():
        ready = sorter.get_ready()
        max_parallelism = max(max_parallelism, len(ready))
        depth += 1
        sorter.done(*ready)
    tools = [node for node in nodes.values() if node.node_type == "tool_use"]
    accesses = [resolved.routes[node.name].tool_access for node in tools]
    unique = {node.name: resolved.routes[node.name].tool_access for node in tools}
    return {
        "total_nodes": len(nodes), "tool_use_count": len(tools),
        "stateful_tool_ratio": sum(access.is_stateful for access in unique.values()) / len(unique) if unique else 0,
        "stateful_invocation_ratio": sum(access.is_stateful for access in accesses) / len(tools) if tools else 0,
        "rw_ratio": sum(access.access == "rw" for access in accesses) / len(tools) if tools else 0,
        "unique_state_domains": sorted({access.domain for access in accesses if access.is_stateful}),
        "dag_depth": depth, "max_parallelism": max_parallelism,
        "parallelism_definition": "maximum topological generation width; structural estimate",
        "tool_call_density": len(tools) / len(nodes),
    }
