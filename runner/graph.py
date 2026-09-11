"""Step graph for a workflow: layered layout + SVG rendering. Pure functions.

Layout: rank = BFS depth from the first step (a step's rank is fixed on first
discovery, so back-edges never move nodes). The builtin terminals `done` and
`halt` sit in the last column. Nodes within a rank stack in discovery order.

Rendering takes `marks` — {step_id: css_class} — so the same drawing serves
the workflow page (no marks) and an input page (visited / current / halted).
"""
from html import escape

from .workflow import Workflow

NODE_W = 160
NODE_H = 44
COL_GAP = 90
ROW_GAP = 22
PAD = 16
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


def layout(workflow: Workflow) -> dict:
    """{"nodes": {id: {"rank", "row", "kind"}}, "edges": [...], "cols": int, "rows": int}."""
    edges = edges_of(workflow)
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
    return {
        "nodes": nodes,
        "edges": edges,
        "cols": max(rank.values()) + 1,
        "rows": max(rows.values()) if rows else 0,
    }


def _center(node: dict) -> tuple:
    x = PAD + node["rank"] * (NODE_W + COL_GAP)
    y = PAD + node["row"] * (NODE_H + ROW_GAP)
    return x, y


def render_svg(workflow: Workflow, marks: dict, links: dict) -> str:
    """SVG of the step graph. `links` maps step id → href for clickable nodes (child workflows)."""
    lay = layout(workflow)
    width = PAD * 2 + lay["cols"] * NODE_W + (lay["cols"] - 1) * COL_GAP
    height = PAD * 2 + lay["rows"] * NODE_H + max(lay["rows"] - 1, 0) * ROW_GAP
    parts = [
        f"<svg class='graph' xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
        f"viewBox='0 0 {width} {height}' role='img' aria-label='{escape(workflow.name)} step graph'>",
        "<defs><marker id='arrow' viewBox='0 0 10 10' refX='10' refY='5' markerWidth='8' markerHeight='8' orient='auto-start-reverse'>"
        "<path d='M0,0 L10,5 L0,10 z' class='arrowhead'/></marker></defs>",
    ]
    for src, dst, label in lay["edges"]:
        sx, sy = _center(lay["nodes"][src])
        dx, dy = _center(lay["nodes"][dst])
        x1, y1 = sx + NODE_W, sy + NODE_H / 2
        x2, y2 = dx, dy + NODE_H / 2
        back = lay["nodes"][dst]["rank"] <= lay["nodes"][src]["rank"]
        if back:  # loop: leave from the top, arc over, enter from the top
            x1, y1 = sx + NODE_W / 2, sy
            x2, y2 = dx + NODE_W / 2, dy
            cy = min(y1, y2) - NODE_H
            d = f"M{x1},{y1} C{x1},{cy} {x2},{cy} {x2},{y2}"
            lx, ly = (x1 + x2) / 2, cy + 4
        else:
            mx = (x1 + x2) / 2
            d = f"M{x1},{y1} C{mx},{y1} {mx},{y2} {x2},{y2}"
            lx, ly = mx, (y1 + y2) / 2 - 6
        cls = "edge back" if back else "edge"
        parts.append(f"<path class='{cls}' d='{d}' marker-end='url(#arrow)'/>")
        if label:
            parts.append(f"<text class='edge-label' x='{lx}' y='{ly}' text-anchor='middle'>{escape(label)}</text>")
    for node_id, node in lay["nodes"].items():
        x, y = _center(node)
        classes = ["node", f"kind-{node['kind']}"]
        if node_id in TERMINALS:
            classes.append("terminal")
        if node_id in marks:
            classes.append(marks[node_id])
        body = (
            f"<rect x='{x}' y='{y}' width='{NODE_W}' height='{NODE_H}' rx='8'/>"
            f"<text x='{x + NODE_W / 2}' y='{y + 19}' text-anchor='middle' class='node-id'>{escape(node_id)}</text>"
            f"<text x='{x + NODE_W / 2}' y='{y + 35}' text-anchor='middle' class='node-kind'>{escape(node['kind'])}</text>"
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
