"""Console headings for orchestration transitions."""

NODE_LABELS = {
    "specs": "USER SPECS",
    "generate": "RTL GENERATION",
    "bist": "BIST",
    "validation": "VERIFICATION",
    "backend": "PHYSICAL DESIGN",
    "debug": "DEBUG MODE",
    "lec": "LOGICAL EQUIVALENCE CHECK (LEC)",
    "remote-check": "SSH / SLURM PREFLIGHT",
}


def print_node_header(node: str) -> None:
    title = f"SWITCHING TO {NODE_LABELS.get(node, node.upper())}"
    width = max(80, len(title) + 4)
    border = "=" * width
    print(f"\n{border}\n{border}\n{title.center(width)}\n{border}\n{border}\n", flush=True)
