"""Step graph for a workflow: layered layout + SVG rendering. Pure functions.

Layout: rank = BFS depth from the first step (a step's rank is fixed on first
discovery, so back-edges never move nodes). The builtin terminals `done` and
`halt` sit in the last column. Nodes within a rank stack in discovery order.

Edges: parallel transitions (same src → dst) are merged into one line with
stacked labels. Each node fans its outgoing edges across distinct ports on
its right side (incoming on the left) so lines never share a start point.
Back-edges (to the same or an earlier column) arc over the top of the graph;
the canvas reserves headroom for them.

Rendering takes `marks` — {step_id: css_class} — so the same drawing serves
the workflow page (no marks) and an input page (visited / current / halted).
"""
from html import escape

from .workflow import Workflow

NODE_W = 160
NODE_H = 48
COL_GAP = 150
ROW_GAP = 44
PAD = 16
LOOP_RISE = 30  # vertical headroom per back-edge lane
LABEL_LINE = 13
TERMINALS = ("done", "halt")


def edges_of(workflow: Workflow) -> list:
    """(src, dst, label) for every transition, including implicit `done`."""
    found = []
    for step in workflow.steps.values():
        if step.route:
            for value, target in step.route.items():
                found.append((step.id, target, value))
        elif step.next_step:
            found.append((step.id, step.next_step, ""))
        else:
            found.append((step.id, "done", ""))
    return found


def merged_edges(workflow: Workflow) -> list:
    """(src, dst, [labels]) — parallel transitions collapsed, order preserved."""
    merged = {}
    for src, dst, label in edges_of(workflow):
        merged.setdefault((src, dst), [])
        if label:
            merged[(src, dst)].append(label)
    return [(src, dst, labels) for (src, dst), labels in merged.items()]


def layout(workflow: Workflow) -> dict:
    """{"nodes": {id: {"rank", "row", "kind"}}, "edges": [...merged...], "cols", "rows", "back"}."""
    edges = merged_edges(workflow)
    out = {}
    for src, dst, _ in edges:
        out.setdefault(src, []).append(dst)
    rank = {workflow.first_step: 0}
    queue = [workflow.first_step]
    while queue:
        node = queue.pop(0)
        for dst in out.get(node, []):
            if dst in rank or dst in TERMINALS:
                continue
            rank[dst] = rank[node] + 1
            queue.append(dst)
    for step_id in workflow.steps:  # unreachable steps still get drawn, last column
        rank.setdefault(step_id, max(rank.values()) + 1)
    last = max(rank.values()) + 1
    used_terminals = [t for t in TERMINALS if any(dst == t for _, dst, _ in edges)]
    for t in used_terminals:
        rank[t] = last
    rows = {}
    nodes = {}
    for node_id in list(workflow.steps) + used_terminals:
        r = rank[node_id]
        nodes[node_id] = {
            "rank": r,
            "row": rows.get(r, 0),
            "kind": workflow.steps[node_id].kind if node_id in workflow.steps else node_id,
        }
        rows[r] = rows.get(r, 0) + 1
    back = sum(1 for src, dst, _ in edges if nodes[dst]["rank"] < nodes[src]["rank"] or src == dst)
    return {
        "nodes": nodes,
        "edges": edges,
        "cols": max(rank.values()) + 1,
        "rows": max(rows.values()) if rows else 0,
        "back": back,
    }


def _origin(node: dict, top: int) -> tuple:
    x = PAD + node["rank"] * (NODE_W + COL_GAP)
    y = top + node["row"] * (NODE_H + ROW_GAP)
    return x, y


def _ports(lay: dict) -> tuple:
    """Per-node y-offsets for outgoing (right side) and incoming (left side) forward edges."""
    outgoing, incoming = {}, {}
    for src, dst, _ in lay["edges"]:
        if lay["nodes"][dst]["rank"] <= lay["nodes"][src]["rank"]:
            continue  # loops arc over the top; same-column edges are vertical
        outgoing.setdefault(src, []).append(dst)
        incoming.setdefault(dst, []).append(src)

    def spread(node_id: str, others: list) -> dict:
        ordered = sorted(others, key=lambda o: (lay["nodes"][o]["row"], lay["nodes"][o]["rank"]))
        n = len(ordered)
        return {o: NODE_H * (i + 1) / (n + 1) for i, o in enumerate(ordered)}

    out_ports = {n: spread(n, ts) for n, ts in outgoing.items()}
    in_ports = {n: spread(n, ss) for n, ss in incoming.items()}
    return out_ports, in_ports


def _bezier(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
    u = 1 - t
    return u * u * u * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t * p3


def _point_at_x(x1: float, y1: float, x2: float, y2: float, x: float) -> tuple:
    """Point on the S-curve M x1,y1 C mx,y1 mx,y2 x2,y2 at horizontal position x (bisection)."""
    mx = (x1 + x2) / 2
    lo, hi = 0.0, 1.0
    for _ in range(30):
        t = (lo + hi) / 2
        if _bezier(x1, mx, mx, x2, t) < x:
            lo = t
        else:
            hi = t
    t = (lo + hi) / 2
    return _bezier(x1, mx, mx, x2, t), _bezier(y1, y1, y2, y2, t)


def _label(x: float, y: float, labels: list, anchor: str) -> str:
    """Stacked label lines; (x, y) is the baseline of the first line."""
    if not labels:
        return ""
    spans = "".join(
        f"<tspan x='{x}' y='{y + i * LABEL_LINE}'>{escape(l)}</tspan>" for i, l in enumerate(labels)
    )
    return f"<text class='edge-label' text-anchor='{anchor}'>{spans}</text>"


def render_svg(workflow: Workflow, marks: dict, links: dict) -> str:
    """SVG of the step graph. `links` maps step id → href for clickable nodes (child workflows)."""
    lay = layout(workflow)
    top = PAD + lay["back"] * LOOP_RISE
    width = PAD * 2 + lay["cols"] * NODE_W + (lay["cols"] - 1) * COL_GAP
    height = top + PAD + lay["rows"] * NODE_H + max(lay["rows"] - 1, 0) * ROW_GAP
    out_ports, in_ports = _ports(lay)
    parts = [
        f"<svg class='graph' xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
        f"viewBox='0 0 {width} {height}' role='img' aria-label='{escape(workflow.name)} step graph'>",
        "<defs><marker id='arrow' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'>"
        "<path d='M0,0 L10,5 L0,10 z' class='arrowhead'/></marker></defs>",
    ]
    lane = 0
    for src, dst, labels in lay["edges"]:
        s_node, d_node = lay["nodes"][src], lay["nodes"][dst]
        sx, sy = _origin(s_node, top)
        dx, dy = _origin(d_node, top)
        if src == dst:  # self-loop: small arc over the node
            lane += 1
            apex = top - lane * LOOP_RISE + 6
            x1, x2 = sx + NODE_W * 0.6, sx + NODE_W * 0.4
            d = f"M{x1},{sy} C{x1 + 20},{apex} {x2 - 20},{apex} {x2},{sy}"
            parts.append(f"<path class='edge back' d='{d}' marker-end='url(#arrow)'/>")
            parts.append(_label((x1 + x2) / 2, apex + 4, labels, "middle"))
        elif d_node["rank"] < s_node["rank"]:  # back-edge: arc over the top
            lane += 1
            apex = top - lane * LOOP_RISE + 6
            x1, x2 = sx + NODE_W * 0.3, dx + NODE_W * 0.7
            d = f"M{x1},{sy} C{x1},{apex} {x2},{apex} {x2},{dy}"
            parts.append(f"<path class='edge back' d='{d}' marker-end='url(#arrow)'/>")
            parts.append(_label((x1 + x2) / 2, apex + 4, labels, "middle"))
        elif d_node["rank"] == s_node["rank"]:  # same column: vertical between neighbours
            down = d_node["row"] > s_node["row"]
            x = sx + NODE_W / 2
            y1 = sy + NODE_H if down else sy
            y2 = dy if down else dy + NODE_H
            parts.append(f"<path class='edge' d='M{x},{y1} L{x},{y2}' marker-end='url(#arrow)'/>")
            parts.append(_label(x + 8, (y1 + y2) / 2 + 4, labels, "start"))
        else:  # forward: S-curve between fanned ports
            x1, y1 = sx + NODE_W, sy + out_ports[src][dst]
            x2, y2 = dx, dy + in_ports[dst][src]
            mx = (x1 + x2) / 2
            d = f"M{x1},{y1} C{mx},{y1} {mx},{y2} {x2},{y2}"
            parts.append(f"<path class='edge' d='{d}' marker-end='url(#arrow)'/>")
            mid_y = (y1 + y2) / 2
            span = d_node["rank"] - s_node["rank"]
            if abs(y2 - y1) <= NODE_H:  # near-flat: label centred above the line
                parts.append(_label(mx, mid_y - 8 - LABEL_LINE * (len(labels) - 1), labels, "middle"))
            elif span > 1:  # passes under other columns: label in the first gap, off the curve
                gx, gy = _point_at_x(x1, y1, x2, y2, x1 + COL_GAP / 2)
                if y2 > y1:
                    parts.append(_label(gx, gy + 16, labels, "middle"))
                else:
                    parts.append(_label(gx, gy - 10 - LABEL_LINE * (len(labels) - 1), labels, "middle"))
            elif y2 > y1:  # descends to the right: the up-right quadrant is clear
                parts.append(_label(mx + 8, mid_y - 6 - LABEL_LINE * (len(labels) - 1), labels, "start"))
            else:  # ascends to the right: the down-right quadrant is clear
                parts.append(_label(mx + 8, mid_y + 14, labels, "start"))
    for node_id, node in lay["nodes"].items():
        x, y = _origin(node, top)
        classes = ["node", f"kind-{node['kind']}"]
        if node_id in TERMINALS:
            classes.append("terminal")
        if node_id in marks:
            classes.append(marks[node_id])
        body = (
            f"<rect x='{x}' y='{y}' width='{NODE_W}' height='{NODE_H}' rx='8'/>"
            f"<text x='{x + NODE_W / 2}' y='{y + 20}' text-anchor='middle' class='node-id'>{escape(node_id)}</text>"
            f"<text x='{x + NODE_W / 2}' y='{y + 37}' text-anchor='middle' class='node-kind'>{escape(node['kind'])}</text>"
        )
        if node_id in links:
            body = f"<a href='{escape(links[node_id])}'>{body}</a>"
        parts.append(f"<g class='{' '.join(classes)}'>{body}</g>")
    parts.append("</svg>")
    return "".join(parts)


def marks_for(state_status: str, current_step: str, history: list) -> dict:
    """Which nodes to highlight for an input: visited steps, and the current one by status."""
    marks = {}
    for entry in history:
        if entry["event"] == "step_done":
            marks[entry["detail"]["step"]] = "visited"
    if state_status == "DONE":
        marks["done"] = "reached"
        return marks
    if state_status == "HALTED":
        marks[current_step] = "halted"
        marks["halt"] = "reached"
    elif state_status == "WAITING_DECISION":
        marks[current_step] = "waiting"
    else:
        marks[current_step] = "current"
    return marks
