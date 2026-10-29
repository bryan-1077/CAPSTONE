"""Console headings and timestamp formatting for orchestration output."""

from datetime import datetime, timezone


class TimestampedOutput:
    """Prefix nonempty lines while allowing partial prompts through immediately."""

    def __init__(self):
        self.line_start = True

    def format(self, text: str) -> str:
        output = []
        for character in text:
            if character in "\r\n":
                self.line_start = True
            elif self.line_start:
                timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
                output.append(f"[{timestamp}] ")
                self.line_start = False
            output.append(character)
        return "".join(output)

NODE_LABELS = {
    "specs": "USER SPECS",
    "generate": "RTL GENERATION",
    "bist": "BIST",
    "validation": "VERIFICATION",
    "backend": "PHYSICAL DESIGN",
    "gdsii-validation": "GDSII VALIDATION",
    "debug": "DEBUG MODE",
    "lec": "LOGICAL EQUIVALENCE CHECK (LEC)",
    "remote-check": "SSH / SLURM PREFLIGHT",
}


def print_node_header(node: str) -> None:
    title = f"SWITCHING TO {NODE_LABELS.get(node, node.upper())}"
    width = max(80, len(title) + 4)
    border = "=" * width
    print(f"\n{border}\n{border}\n{title.center(width)}\n{border}\n{border}\n", flush=True)
