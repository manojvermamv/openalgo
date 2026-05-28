import ast
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone


ROOT = os.path.abspath(os.path.dirname(__file__))
SCRIPT = os.path.join(ROOT, "BuyerEdgeStrategy.py")
LOGS = os.path.join(ROOT, "Logs.txt")
OUT_DIR = os.path.join(ROOT, ".understand-anything")
GRAPH_PATH = os.path.join(OUT_DIR, "knowledge-graph.json")
HTML_PATH = os.path.join(OUT_DIR, "intraday-trading-algo-graph.html")
META_PATH = os.path.join(OUT_DIR, "meta.json")


def line_count(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)


def add_node(nodes, node_id, node_type, name, summary, tags, file_path=None, **extra):
    node = {
        "id": node_id,
        "type": node_type,
        "name": name,
        "summary": summary,
        "tags": tags,
        "complexity": extra.pop("complexity", "moderate"),
    }
    if file_path:
        node["filePath"] = file_path
    node.update(extra)
    nodes[node_id] = node


def edge(edges, source, target, edge_type, weight=0.6, label=None):
    item = {"source": source, "target": target, "type": edge_type, "weight": weight}
    if label:
        item["label"] = label
    edges.append(item)


def method_summary(class_name, method_name):
    summaries = {
        "from_env": "Builds typed configuration from environment variables and applies exchange, risk, timing, and data defaults.",
        "validate": "Validates startup configuration and halts on unsafe or inconsistent values.",
        "score": "Computes the five-layer directional score, trap score, signal state, direction, and component explanations.",
        "smooth_chain_rows": "Smooths option-chain snapshots and adds price, volume, and OI trend flags per strike.",
        "classify_ce_flow": "Classifies call-side price-volume-OI behavior into bullish, bearish, or neutral flow states.",
        "classify_pe_flow": "Classifies put-side price-volume-OI behavior into underlying bullish, bearish, or neutral flow states.",
        "fetch_option_chain": "Fetches and flattens OpenAlgo option-chain rows into CE/PE fields consumed by scoring and strike selection.",
        "fetch_gex_levels": "Builds a per-strike gamma exposure profile from greeks and OI, then derives GEX levels.",
        "derive_gex_levels": "Calculates gamma flip, walls, punch targets, and total net GEX from per-strike net exposure.",
        "fetch_atm_iv_ranks": "Fetches CE/PE implied volatility ranks and identifies the cheaper side for option buying.",
        "resolve_entry_sl_points": "Chooses fixed, strike ATR, or spot ATR stop points and adapts stop width by delta when available.",
        "select_best": "Filters candidate strikes by signal strength, liquidity, IV rank, delta fit, and asymmetry score.",
        "check_gates": "Applies session risk guards including trade count, loss streak, cooldown, daily loss/profit, and timing.",
        "available_capital": "Caches broker funds and adjusts available capital by local PnL between refreshes.",
        "place_entry": "Places or simulates a BUY, applies preflight liquidity checks, polls fills, and registers the position.",
        "place_exit": "Cancels protection orders when needed, places or simulates SELL exit, records PnL, and cleans state.",
        "register_filled_entry": "Creates tracked OptionPosition state with SL, target, moneyness, subscriptions, and broker protection.",
        "check_pending_entries": "Reconciles pending BUY orders and protects against post-cutoff fills.",
        "check_pending_exits": "Reconciles pending SELL exits and releases retry state on rejected exits.",
        "_on_ws_data": "Stores live LTP ticks and fans them out to premium and spot trailing-stop checks.",
        "_check_premium_trail": "Handles premium SL, target, breakeven, and premium trail ratchets from option LTP ticks.",
        "_check_spot_trail": "Handles spot-based trailing stop activation and ratchets for CE/PE positions.",
        "_trigger_exit": "Marks a position exit-pending and starts the configured exit callback in a separate thread.",
        "_ws_thread": "Maintains websocket connection, replays subscriptions, and reconnects after feed silence or failures.",
        "scan_underlying": "Runs the full per-underlying scan: data fetch, smoothing, scoring, strike selection, sizing, and entry.",
        "_strategy_thread": "Runs the clock-anchored loop for reconciliation, square-off, hold-time exits, and scans.",
        "_test_websocket": "Smoke-tests REST auth, websocket transport, authentication, subscription, and tick delivery.",
        "run": "Starts registration checks, position restoration, websocket feed, strategy thread, and shutdown handling.",
    }
    return summaries.get(method_name, f"{class_name}.{method_name} participates in the trading bot control flow.")


def classify_layer(name):
    if name in {"BotConfig"}:
        return "layer:configuration"
    if name in {"ScoreComponent", "SignalResult", "OptionPosition", "PendingEntry", "PendingExit", "BotState"}:
        return "layer:state-model"
    if name in {"OIFlowAnalyzer", "SignalEngine"}:
        return "layer:signal-intelligence"
    if name in {"DataFetcher"}:
        return "layer:market-data"
    if name in {"EntryStopLossPolicy", "StrikeSelector", "RiskManager"}:
        return "layer:risk-and-selection"
    if name in {"WebSocketManager", "OrderManager"}:
        return "layer:execution-and-protection"
    if name in {"OptionsBuyerEdgeBot"}:
        return "layer:orchestration"
    return "layer:utilities"


def build_graph():
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(SCRIPT, encoding="utf-8", errors="replace") as f:
        source = f.read()
    with open(LOGS, encoding="utf-8", errors="replace") as f:
        logs = f.read()
    tree = ast.parse(source)
    nodes = {}
    edges = []

    add_node(
        nodes,
        "file:BuyerEdgeStrategy.py",
        "file",
        "BuyerEdgeStrategy.py",
        "Single-file OpenAlgo NSE options buying bot with configuration, data fetch, five-layer scoring, strike selection, risk sizing, websocket protection, and order management.",
        ["python", "trading-bot", "openalgo", "intraday", "options"],
        "BuyerEdgeStrategy.py",
        language="python",
        sizeLines=line_count(SCRIPT),
        complexity="complex",
    )
    add_node(
        nodes,
        "document:Logs.txt",
        "document",
        "Logs.txt",
        "Runtime log from the BuyerEdgeStrategyBot showing startup, websocket smoke test, repeated NIFTY scans, signal panels, risk sizing blocks, and outside-market-hours loop behavior.",
        ["logs", "runtime-evidence", "nifty", "diagnostics"],
        "Logs.txt",
        sizeLines=line_count(LOGS),
        complexity="moderate",
    )

    class_methods = {}
    class_layers = {}
    top_functions = []
    dataclasses = {"BotConfig", "ScoreComponent", "SignalResult", "OptionPosition", "PendingEntry", "PendingExit"}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            cid = f"class:BuyerEdgeStrategy.py:{node.name}"
            methods = [m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            class_methods[node.name] = [m.name for m in methods]
            class_layers[node.name] = classify_layer(node.name)
            tag = "dataclass" if node.name in dataclasses else "component"
            add_node(
                nodes,
                cid,
                "class",
                node.name,
                {
                    "BotConfig": "Typed runtime configuration surface for API connection, universe, scoring, risk, timing, trailing, and execution flags.",
                    "BotState": "Thread-safe mutable state for positions, LTP map, pending orders, chain history, prior straddles, and same-strike guards.",
                    "OIFlowAnalyzer": "Static option-chain intelligence helper for smoothing and CE/PE flow classification.",
                    "SignalEngine": "Composite five-layer scoring engine producing score, trap risk, decision, direction, and reasons.",
                    "DataFetcher": "OpenAlgo market-data adapter with greeks cache, option-chain flattening, candles, quotes, synthetic futures, GEX, IVR, and expiry resolution.",
                    "EntryStopLossPolicy": "Initial stop-loss resolver supporting fixed, strike ATR, spot ATR, and delta-aware stop adaptation.",
                    "StrikeSelector": "Liquidity, IV, delta, and asymmetry based strike selector with simple OTM fallback.",
                    "RiskManager": "Session risk gatekeeper for capital, trade limits, losses, cooldowns, drawdown rate, and adaptive sizing.",
                    "WebSocketManager": "Live LTP feed manager plus premium/spot trailing stop and exit trigger engine.",
                    "OrderManager": "Order lifecycle manager for entry, exit, broker SL/target protection, pending reconciliation, and journal writes.",
                    "OptionsBuyerEdgeBot": "Top-level orchestrator wiring all components and running websocket plus strategy threads.",
                }.get(node.name, f"{node.name} data model used by the trading bot."),
                [tag, class_layers[node.name].replace("layer:", ""), "python"],
                "BuyerEdgeStrategy.py",
                startLine=node.lineno,
                endLine=getattr(node, "end_lineno", node.lineno),
                complexity="complex" if len(methods) > 10 else "moderate",
            )
            edge(edges, "file:BuyerEdgeStrategy.py", cid, "contains", 1.0)
            for m in methods:
                mid = f"function:BuyerEdgeStrategy.py:{node.name}.{m.name}"
                add_node(
                    nodes,
                    mid,
                    "function",
                    f"{node.name}.{m.name}",
                    method_summary(node.name, m.name),
                    ["method", node.name, class_layers[node.name].replace("layer:", "")],
                    "BuyerEdgeStrategy.py",
                    startLine=m.lineno,
                    endLine=getattr(m, "end_lineno", m.lineno),
                    complexity="complex" if (getattr(m, "end_lineno", m.lineno) - m.lineno) > 80 else "moderate",
                )
                edge(edges, cid, mid, "contains", 1.0)
        elif isinstance(node, ast.FunctionDef):
            top_functions.append(node.name)
            fid = f"function:BuyerEdgeStrategy.py:{node.name}"
            add_node(
                nodes,
                fid,
                "function",
                node.name,
                "Utility function used by OI smoothing to classify field direction across snapshots.",
                ["utility", "oi-flow"],
                "BuyerEdgeStrategy.py",
                startLine=node.lineno,
                endLine=getattr(node, "end_lineno", node.lineno),
            )
            edge(edges, "file:BuyerEdgeStrategy.py", fid, "contains", 1.0)

    concepts = {
        "concept:five-layer-confirmation": ("Five-Layer Confirmation", "Entry decision combines technical trend, OI flow, greeks/GEX, IV and straddle behavior, and synthetic futures co-movement.", ["signal", "architecture"]),
        "concept:score-and-trap-gates": ("Score and Trap Gates", "Signals execute only when absolute score clears the effective min score and trap risk remains below the configured max trap.", ["decision", "gate"]),
        "concept:oi-flow-intelligence": ("OI Flow Intelligence", "PCR, CE/PE price-volume-OI classification, OI walls, and OI velocity convert option-chain behavior into directional evidence.", ["oi-flow", "options"]),
        "concept:gex-regime": ("GEX Regime", "Net GEX, gamma flip, walls, and punch targets influence whether price movement is expected to accelerate or dampen.", ["greeks", "gex"]),
        "concept:iv-and-straddle": ("IV and Straddle Layer", "IV rank and straddle velocity estimate whether long options have premium expansion edge or IV crush risk.", ["iv", "straddle"]),
        "concept:synthetic-futures-confirmation": ("Synthetic Futures Confirmation", "Spot and synthetic-future co-movement is used as an extra directional confirmation or divergence warning.", ["synthetic-futures", "confirmation"]),
        "concept:strike-asymmetry-selection": ("Strike Asymmetry Selection", "Candidate strikes are scored by IV cheapness, OI concentration, volume concentration, and delta target fit.", ["strike-selection", "liquidity"]),
        "concept:risk-sizing-block": ("Risk Sizing Block", "Runtime logs show signals were blocked because one NIFTY lot risk exceeded the configured 1 percent risk cap.", ["risk", "runtime-finding"]),
        "concept:runtime-data-quality": ("Runtime Data Quality", "Logs show VWAP volume and IVR were unavailable on every scan, muting two scoring layers.", ["logs", "data-quality"]),
        "concept:websocket-protection": ("WebSocket Protection Loop", "Live ticks update LTP state and drive premium SL, target, breakeven, spot trail, and deep-OTM delta exits.", ["websocket", "protection"]),
        "concept:order-reconciliation": ("Order Reconciliation", "Pending entries/exits and broker SL/target fills are polled to prevent orphaned order state.", ["orders", "reconciliation"]),
    }
    for cid, (name, summary, tags) in concepts.items():
        add_node(nodes, cid, "concept", name, summary, tags, complexity="moderate")

    # Major component relationships.
    component_edges = [
        ("class:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot", "class:BuyerEdgeStrategy.py:BotConfig", "configures"),
        ("class:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot", "class:BuyerEdgeStrategy.py:BotState", "depends_on"),
        ("class:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot", "class:BuyerEdgeStrategy.py:RiskManager", "depends_on"),
        ("class:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot", "class:BuyerEdgeStrategy.py:DataFetcher", "depends_on"),
        ("class:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot", "class:BuyerEdgeStrategy.py:SignalEngine", "depends_on"),
        ("class:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot", "class:BuyerEdgeStrategy.py:StrikeSelector", "depends_on"),
        ("class:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot", "class:BuyerEdgeStrategy.py:EntryStopLossPolicy", "depends_on"),
        ("class:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot", "class:BuyerEdgeStrategy.py:WebSocketManager", "depends_on"),
        ("class:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot", "class:BuyerEdgeStrategy.py:OrderManager", "depends_on"),
        ("class:BuyerEdgeStrategy.py:SignalEngine", "class:BuyerEdgeStrategy.py:OIFlowAnalyzer", "calls"),
        ("class:BuyerEdgeStrategy.py:SignalEngine", "concept:five-layer-confirmation", "defines_schema"),
        ("class:BuyerEdgeStrategy.py:SignalEngine", "concept:score-and-trap-gates", "validates"),
        ("class:BuyerEdgeStrategy.py:OIFlowAnalyzer", "concept:oi-flow-intelligence", "defines_schema"),
        ("class:BuyerEdgeStrategy.py:DataFetcher", "concept:gex-regime", "transforms"),
        ("class:BuyerEdgeStrategy.py:DataFetcher", "concept:iv-and-straddle", "reads_from"),
        ("class:BuyerEdgeStrategy.py:SignalEngine", "concept:gex-regime", "reads_from"),
        ("class:BuyerEdgeStrategy.py:SignalEngine", "concept:iv-and-straddle", "reads_from"),
        ("class:BuyerEdgeStrategy.py:SignalEngine", "concept:synthetic-futures-confirmation", "reads_from"),
        ("class:BuyerEdgeStrategy.py:StrikeSelector", "concept:strike-asymmetry-selection", "defines_schema"),
        ("class:BuyerEdgeStrategy.py:RiskManager", "concept:risk-sizing-block", "validates"),
        ("class:BuyerEdgeStrategy.py:WebSocketManager", "concept:websocket-protection", "triggers"),
        ("class:BuyerEdgeStrategy.py:OrderManager", "concept:order-reconciliation", "validates"),
        ("document:Logs.txt", "concept:risk-sizing-block", "documents"),
        ("document:Logs.txt", "concept:runtime-data-quality", "documents"),
    ]
    for s, t, typ in component_edges:
        edge(edges, s, t, typ, 0.8 if typ in {"calls", "defines_schema"} else 0.6)

    # Method-level critical path.
    path_edges = [
        ("function:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot.run", "function:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot._test_websocket", "calls"),
        ("function:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot.run", "function:BuyerEdgeStrategy.py:WebSocketManager.start", "calls"),
        ("function:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot.run", "function:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot._strategy_thread", "calls"),
        ("function:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot._strategy_thread", "function:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot.scan_underlying", "calls"),
        ("function:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot.scan_underlying", "function:BuyerEdgeStrategy.py:DataFetcher.fetch_quote", "calls"),
        ("function:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot.scan_underlying", "function:BuyerEdgeStrategy.py:DataFetcher.fetch_option_chain", "calls"),
        ("function:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot.scan_underlying", "function:BuyerEdgeStrategy.py:OIFlowAnalyzer.smooth_chain_rows", "calls"),
        ("function:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot.scan_underlying", "function:BuyerEdgeStrategy.py:SignalEngine.score", "calls"),
        ("function:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot.scan_underlying", "function:BuyerEdgeStrategy.py:StrikeSelector.select_best", "calls"),
        ("function:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot.scan_underlying", "function:BuyerEdgeStrategy.py:RiskManager.available_capital", "calls"),
        ("function:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot.scan_underlying", "function:BuyerEdgeStrategy.py:OrderManager.place_entry", "calls"),
        ("function:BuyerEdgeStrategy.py:WebSocketManager._on_ws_data", "function:BuyerEdgeStrategy.py:WebSocketManager._check_premium_trail", "calls"),
        ("function:BuyerEdgeStrategy.py:WebSocketManager._on_ws_data", "function:BuyerEdgeStrategy.py:WebSocketManager._check_spot_trail", "calls"),
        ("function:BuyerEdgeStrategy.py:WebSocketManager._trigger_exit", "function:BuyerEdgeStrategy.py:OrderManager.place_exit", "triggers"),
        ("function:BuyerEdgeStrategy.py:OrderManager.place_entry", "function:BuyerEdgeStrategy.py:OrderManager.register_filled_entry", "calls"),
        ("function:BuyerEdgeStrategy.py:OrderManager.register_filled_entry", "class:BuyerEdgeStrategy.py:OptionPosition", "writes_to"),
        ("function:BuyerEdgeStrategy.py:OrderManager.place_exit", "function:BuyerEdgeStrategy.py:OrderManager._write_journal", "calls"),
    ]
    for s, t, typ in path_edges:
        if s in nodes and t in nodes:
            edge(edges, s, t, typ, 0.8)

    stats = {
        "scan_blocks": len(re.findall(r"SCAN . NIFTY|SCAN · NIFTY", logs)),
        "execute_lines": len(re.findall(r"✔ EXECUTE", logs)),
        "no_trade_lines": len(re.findall(r"✘ NO_TRADE", logs)),
        "qty_zero": len(re.findall(r"qty=0", logs)),
        "risk_exceeds": len(re.findall(r"risk exceeds cap", logs)),
        "signal_lt_40": len(re.findall(r"Signal score .* < 40", logs)),
        "asymmetry_fail": len(re.findall(r"Best asymmetry score .* < threshold", logs)),
        "low_vwap_volume": len(re.findall(r"\[VWAP\] Low volume", logs)),
        "ivr_unavailable": len(re.findall(r"IV Regime \(IVR\).*IVR unavailable", logs)),
        "entry_orders": len(re.findall(r"Entry order .* placed|Simulated BUY", logs)),
        "exit_orders": len(re.findall(r"Exit order .* placed|Simulated SELL", logs)),
        "outside_hours": len(re.findall(r"Outside market hours", logs)),
    }
    add_node(
        nodes,
        "concept:log-run-summary",
        "concept",
        "Runtime Log Summary",
        (
            f"Observed {stats['scan_blocks']} scan panels, {stats['qty_zero']} qty-zero risk blocks, "
            f"{stats['entry_orders']} entry orders, and {stats['exit_orders']} exit orders. "
            "The run scored signals but did not place trades."
        ),
        ["runtime-summary", "logs", "finding"],
        stats=stats,
        complexity="simple",
    )
    edge(edges, "document:Logs.txt", "concept:log-run-summary", "documents", 0.8)
    edge(edges, "concept:log-run-summary", "concept:risk-sizing-block", "related", 0.7)
    edge(edges, "concept:log-run-summary", "concept:runtime-data-quality", "related", 0.7)

    layer_defs = {
        "layer:configuration": ("Configuration", "Environment-driven strategy, execution, risk, timing, and routing settings."),
        "layer:state-model": ("State and Models", "Dataclasses and shared mutable state that represent scores, positions, pending orders, and chain history."),
        "layer:market-data": ("Market Data and Feature Extraction", "OpenAlgo data access plus greeks, GEX, IVR, expiry, candles, quotes, and synthetic futures."),
        "layer:signal-intelligence": ("Signal Intelligence", "OI flow smoothing/classification and five-layer scoring with trap detection."),
        "layer:risk-and-selection": ("Risk, Stops, and Strike Selection", "Entry stop policy, strike asymmetry filters, capital sizing, and session risk gates."),
        "layer:execution-and-protection": ("Execution and Protection", "Websocket tick handling, trailing exits, order placement, broker protection, and reconciliation."),
        "layer:orchestration": ("Bot Orchestration", "Startup, component wiring, scan loop, websocket smoke test, square-off, and shutdown."),
        "layer:runtime-evidence": ("Runtime Evidence", "Logs and derived findings from the observed run."),
    }
    layer_nodes = {k: [] for k in layer_defs}
    layer_nodes["layer:runtime-evidence"].extend(["document:Logs.txt", "concept:log-run-summary", "concept:risk-sizing-block", "concept:runtime-data-quality"])
    for nid, n in nodes.items():
        if nid == "file:BuyerEdgeStrategy.py":
            layer_nodes["layer:orchestration"].append(nid)
        elif nid.startswith("class:BuyerEdgeStrategy.py:"):
            cname = nid.split(":")[-1]
            layer_nodes[class_layers.get(cname, "layer:orchestration")].append(nid)
        elif nid.startswith("function:BuyerEdgeStrategy.py:"):
            parts = nid.split(":")[-1].split(".")
            cname = parts[0] if len(parts) > 1 else ""
            layer_nodes[class_layers.get(cname, "layer:signal-intelligence")].append(nid)
        elif nid.startswith("concept:") and nid not in layer_nodes["layer:runtime-evidence"]:
            if "websocket" in nid or "order" in nid:
                layer_nodes["layer:execution-and-protection"].append(nid)
            elif "strike" in nid or "risk" in nid:
                layer_nodes["layer:risk-and-selection"].append(nid)
            else:
                layer_nodes["layer:signal-intelligence"].append(nid)

    layers = [
        {"id": lid, "name": name, "description": desc, "nodeIds": sorted(set(layer_nodes[lid]))}
        for lid, (name, desc) in layer_defs.items()
    ]
    tour = [
        {"order": 1, "title": "Start at the Bot", "description": "Begin with the top-level orchestrator and entry point to see how the bot wires all components and starts the scan/websocket loops.", "nodeIds": ["file:BuyerEdgeStrategy.py", "class:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot", "function:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot.run"]},
        {"order": 2, "title": "Follow the Scan Pipeline", "description": "Trace scan_underlying through quote, option-chain, smoothing, scoring, strike selection, sizing, and entry.", "nodeIds": ["function:BuyerEdgeStrategy.py:OptionsBuyerEdgeBot.scan_underlying", "class:BuyerEdgeStrategy.py:DataFetcher", "class:BuyerEdgeStrategy.py:SignalEngine", "class:BuyerEdgeStrategy.py:StrikeSelector"]},
        {"order": 3, "title": "Understand Signal Construction", "description": "Inspect the five independent confirmation layers and how they become a score, trap score, signal, and CE/PE direction.", "nodeIds": ["concept:five-layer-confirmation", "concept:score-and-trap-gates", "class:BuyerEdgeStrategy.py:OIFlowAnalyzer", "function:BuyerEdgeStrategy.py:SignalEngine.score"]},
        {"order": 4, "title": "Inspect Risk and Trade Blocking", "description": "The observed run repeatedly reached executable signals but was blocked by lot-risk sizing; this is the central runtime finding.", "nodeIds": ["class:BuyerEdgeStrategy.py:RiskManager", "concept:risk-sizing-block", "concept:log-run-summary"]},
        {"order": 5, "title": "Review Live Protection", "description": "When entries exist, websocket ticks drive premium SL, target, breakeven, spot trail, and exit callbacks.", "nodeIds": ["class:BuyerEdgeStrategy.py:WebSocketManager", "concept:websocket-protection", "class:BuyerEdgeStrategy.py:OrderManager"]},
        {"order": 6, "title": "Use Logs as Feedback", "description": "The log evidence shows missing IVR and VWAP volume on every scan, plus no order placements in this run.", "nodeIds": ["document:Logs.txt", "concept:runtime-data-quality", "concept:log-run-summary"]},
    ]
    commit = os.popen(f"git -C {json.dumps(ROOT)} rev-parse HEAD 2>/dev/null").read().strip()
    graph = {
        "version": "1.0.0",
        "project": {
            "name": "BuyerEdgeStrategy Intraday Options Bot",
            "languages": ["python", "text"],
            "frameworks": ["OpenAlgo SDK", "pandas"],
            "description": "In-depth structure graph for a single-file intraday NSE F&O options buying algorithm and its observed runtime logs.",
            "analyzedAt": datetime.now(timezone.utc).isoformat(),
            "gitCommitHash": commit or "unknown",
        },
        "nodes": list(nodes.values()),
        "edges": edges,
        "layers": layers,
        "tour": tour,
    }
    with open(GRAPH_PATH, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2)
    meta = {
        "lastAnalyzedAt": graph["project"]["analyzedAt"],
        "gitCommitHash": graph["project"]["gitCommitHash"],
        "version": graph["version"],
        "analyzedFiles": 2,
        "generation": "manual-understand-schema-fallback",
        "sourceHash": hashlib.sha256((source + logs).encode("utf-8")).hexdigest(),
    }
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    write_html(graph)
    return graph


def write_html(graph):
    graph_json = json.dumps(graph)
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>BuyerEdgeStrategy Intraday Algo Graph</title>
<style>
html, body {{ margin: 0; height: 100%; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background: #101316; color: #e8edf2; }}
#app {{ display: grid; grid-template-columns: 300px 1fr 360px; height: 100vh; }}
aside, section {{ border-color: #2b333b; }}
.left {{ border-right: 1px solid #2b333b; padding: 14px; overflow: auto; background: #151a1f; }}
.right {{ border-left: 1px solid #2b333b; padding: 14px; overflow: auto; background: #151a1f; }}
h1 {{ font-size: 16px; margin: 0 0 12px; }}
h2 {{ font-size: 13px; margin: 18px 0 8px; color: #a8d5ff; }}
input, select {{ width: 100%; box-sizing: border-box; background: #0f1216; color: #e8edf2; border: 1px solid #39434d; border-radius: 6px; padding: 8px; }}
button {{ background: #203040; color: #edf6ff; border: 1px solid #456078; border-radius: 6px; padding: 7px 9px; cursor: pointer; }}
.stats {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
.stat {{ background: #0f1216; border: 1px solid #2b333b; border-radius: 6px; padding: 8px; }}
.stat b {{ display: block; font-size: 18px; }}
.layer {{ display: block; width: 100%; margin: 6px 0; text-align: left; }}
#canvasWrap {{ position: relative; overflow: hidden; }}
svg {{ width: 100%; height: 100%; display: block; background: radial-gradient(circle at 20% 15%, #1b2730 0, #101316 34%); }}
.link {{ stroke: #51606b; stroke-opacity: .55; }}
.node circle {{ stroke: #0e1114; stroke-width: 1.5; cursor: pointer; }}
.node text {{ font-size: 10px; fill: #dce8f3; pointer-events: none; text-shadow: 0 1px 2px #000; }}
.selected circle {{ stroke: #fff; stroke-width: 3; }}
.muted {{ opacity: .14; }}
.pill {{ display: inline-block; margin: 3px 4px 3px 0; padding: 3px 6px; border-radius: 999px; background: #24313b; color: #c8d7e3; font-size: 11px; }}
.mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color: #bcd8f0; word-break: break-word; }}
.finding {{ background: #181f26; border: 1px solid #34414d; border-radius: 6px; padding: 8px; margin: 7px 0; }}
@media (max-width: 980px) {{ #app {{ grid-template-columns: 1fr; grid-template-rows: auto 58vh auto; }} .left, .right {{ border: 0; }} }}
</style>
</head>
<body>
<div id="app">
<aside class="left">
<h1>BuyerEdgeStrategy Graph</h1>
<div class="stats">
<div class="stat"><b id="nodeCount"></b>Nodes</div>
<div class="stat"><b id="edgeCount"></b>Edges</div>
<div class="stat"><b id="layerCount"></b>Layers</div>
<div class="stat"><b id="tourCount"></b>Tour</div>
</div>
<h2>Search</h2>
<input id="search" placeholder="class, concept, method..." />
<h2>Layer</h2>
<select id="layerSelect"><option value="">All layers</option></select>
<h2>Guided Tour</h2>
<div id="tour"></div>
<h2>Runtime Findings</h2>
<div class="finding">94 scan panels, 49 qty-zero blocks, 0 entry orders, 0 exit orders.</div>
<div class="finding">VWAP volume and IVR were unavailable on every scan, reducing signal-layer confidence.</div>
</aside>
<main id="canvasWrap"><svg id="svg"></svg></main>
<section class="right">
<h1 id="detailTitle">Select a node</h1>
<div id="detailType" class="mono"></div>
<p id="detailSummary">Click a node or use search/tour filters.</p>
<div id="detailTags"></div>
<h2>Connected Edges</h2>
<div id="edges"></div>
</section>
</div>
<script>
const graph = {graph_json};
const colors = {{ file:"#59b3ff", class:"#70e0a0", function:"#ffd166", concept:"#f78fb3", document:"#c7a8ff" }};
const svg = document.getElementById("svg");
const W = () => svg.clientWidth || 900, H = () => svg.clientHeight || 700;
let selected = null, activeLayer = "", search = "";
document.getElementById("nodeCount").textContent = graph.nodes.length;
document.getElementById("edgeCount").textContent = graph.edges.length;
document.getElementById("layerCount").textContent = graph.layers.length;
document.getElementById("tourCount").textContent = graph.tour.length;
const layerOf = new Map();
graph.layers.forEach(l => l.nodeIds.forEach(id => layerOf.set(id, l.id)));
const byId = new Map(graph.nodes.map(n => [n.id, n]));
const layerSelect = document.getElementById("layerSelect");
graph.layers.forEach(l => {{
  const o = document.createElement("option"); o.value = l.id; o.textContent = l.name; layerSelect.appendChild(o);
}});
const tour = document.getElementById("tour");
graph.tour.forEach(step => {{
  const b = document.createElement("button"); b.className = "layer"; b.textContent = step.order + ". " + step.title;
  b.onclick = () => {{ selected = step.nodeIds[0]; activeLayer = ""; layerSelect.value = ""; render(); showNode(selected); }};
  tour.appendChild(b);
}});
function layout() {{
  const layers = graph.layers;
  const coords = new Map();
  const width = W(), height = H();
  layers.forEach((layer, li) => {{
    const ids = layer.nodeIds.filter(id => byId.has(id));
    const x = 90 + li * Math.max(120, (width - 180) / Math.max(layers.length - 1, 1));
    ids.forEach((id, i) => {{
      const y = 55 + (i + 1) * ((height - 110) / (ids.length + 1));
      coords.set(id, {{x, y}});
    }});
  }});
  graph.nodes.forEach((n, i) => {{ if (!coords.has(n.id)) coords.set(n.id, {{x: 80 + (i % 9) * 100, y: 80 + Math.floor(i / 9) * 42}}); }});
  return coords;
}}
function visible(n) {{
  const q = search.trim().toLowerCase();
  const matchesSearch = !q || (n.name + " " + n.summary + " " + n.id + " " + (n.tags || []).join(" ")).toLowerCase().includes(q);
  const matchesLayer = !activeLayer || layerOf.get(n.id) === activeLayer;
  if (selected) {{
    const connected = graph.edges.some(e => (e.source === selected && e.target === n.id) || (e.target === selected && e.source === n.id));
    return n.id === selected || connected || (matchesSearch && matchesLayer);
  }}
  return matchesSearch && matchesLayer;
}}
function render() {{
  const coords = layout();
  svg.innerHTML = "";
  const vis = new Set(graph.nodes.filter(visible).map(n => n.id));
  graph.edges.forEach(e => {{
    if (!vis.has(e.source) || !vis.has(e.target)) return;
    const a = coords.get(e.source), b = coords.get(e.target);
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", a.x); line.setAttribute("y1", a.y); line.setAttribute("x2", b.x); line.setAttribute("y2", b.y);
    line.setAttribute("class", "link"); line.setAttribute("stroke-width", Math.max(1, (e.weight || .5) * 2));
    svg.appendChild(line);
  }});
  graph.nodes.forEach(n => {{
    const c = coords.get(n.id), isVis = vis.has(n.id);
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.setAttribute("class", "node" + (selected === n.id ? " selected" : "") + (isVis ? "" : " muted"));
    g.setAttribute("transform", `translate(${{c.x}},${{c.y}})`);
    const r = n.type === "function" ? 5 : n.type === "concept" ? 8 : n.type === "class" ? 9 : 7;
    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("r", r); circle.setAttribute("fill", colors[n.type] || "#9fb0bf");
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", r + 4); text.setAttribute("y", 4); text.textContent = n.name.length > 34 ? n.name.slice(0, 33) + "..." : n.name;
    g.appendChild(circle); g.appendChild(text);
    g.onclick = () => {{ selected = n.id; showNode(n.id); render(); }};
    svg.appendChild(g);
  }});
}}
function showNode(id) {{
  const n = byId.get(id); if (!n) return;
  document.getElementById("detailTitle").textContent = n.name;
  document.getElementById("detailType").textContent = n.type + " | " + n.id + (n.filePath ? " | " + n.filePath : "");
  document.getElementById("detailSummary").textContent = n.summary;
  document.getElementById("detailTags").innerHTML = (n.tags || []).map(t => `<span class="pill">${{t}}</span>`).join("");
  const connected = graph.edges.filter(e => e.source === id || e.target === id).slice(0, 80);
  document.getElementById("edges").innerHTML = connected.map(e => {{
    const other = e.source === id ? e.target : e.source;
    const on = byId.get(other);
    return `<div class="finding"><span class="mono">${{e.type}}</span><br>${{e.source === id ? "to" : "from"}}: ${{on ? on.name : other}}</div>`;
  }}).join("") || "<p>No connected edges.</p>";
}}
document.getElementById("search").oninput = e => {{ search = e.target.value; selected = null; render(); }};
layerSelect.onchange = e => {{ activeLayer = e.target.value; selected = null; render(); }};
window.onresize = render;
render();
</script>
</body>
</html>"""
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    graph = build_graph()
    type_counts = Counter(n["type"] for n in graph["nodes"])
    edge_counts = Counter(e["type"] for e in graph["edges"])
    print(json.dumps({
        "graph": GRAPH_PATH,
        "html": HTML_PATH,
        "nodes": len(graph["nodes"]),
        "edges": len(graph["edges"]),
        "nodeTypes": type_counts,
        "edgeTypes": edge_counts,
        "layers": [l["name"] for l in graph["layers"]],
    }, indent=2))
