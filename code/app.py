import streamlit as st
import pyomo.environ as pyo
import pandas as pd
import networkx as nx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import io
from dataclasses import dataclass
from collections import defaultdict

# ===============================
# PAGE CONFIG
# ===============================
st.set_page_config(page_title="MCNF Optimization", layout="wide")

st.title("🌾 Agricultural Supply Chain Optimization")
st.markdown("### Multi-Commodity Network Flow (MCNF) Model")

st.info("""
Minimizes transportation cost while satisfying supply and demand constraints.
Includes a penalty for unmet demand (shortage cost), waste analysis, bottleneck detection,
sensitivity analysis and network visualizations.
""")

# ===============================
# SIDEBAR SETTINGS
# ===============================
st.sidebar.header("⚙️ Settings")
penalty_multiplier = st.sidebar.slider(
    "Penalty Multiplier (λ)", 1.1, 5.0, 2.0, 0.1,
    help="Shortage cost per ton = (most expensive supply→demand route cost) × λ. "
         "Keep λ above 1 so the solver never prefers shortage to delivery."
)

st.sidebar.subheader("Scenario multipliers")
supply_multiplier = st.sidebar.slider(
    "Supply Multiplier", 0.2, 2.0, 1.0, 0.2,
    help="Scales all supply quantities (farm production).",
)
demand_multiplier = st.sidebar.slider(
    "Demand Multiplier", 0.2, 2.0, 1.0, 0.2,
    help="Scales all demand quantities (market requirements).",
)
capacity_multiplier = st.sidebar.slider(
    "Capacity Multiplier", 0.2, 2.0, 1.0, 0.2,
    help="Scales all route capacities in the edges file (if provided).",
)

# ===============================
# UPLOAD FILES
# ===============================
st.header("📂 Upload Input Data")
col1, col2, col3 = st.columns(3)
with col1:
    supply_file = st.file_uploader("Supply CSV", type=["csv"])
with col2:
    demand_file = st.file_uploader("Demand CSV", type=["csv"])
with col3:
    edges_file = st.file_uploader("Edges CSV", type=["csv"])

# ===============================
# SAFE VALUE HELPER
# ===============================

def safe_val(expr):
    try:
        v = pyo.value(expr)
        return v if v is not None else 0
    except Exception:
        return 0


# ===============================
# CONSTRAINT RULES
# ===============================

def make_supply_rule(supply_nodes, supply_dict):
    def supply_rule(m, n, k):
        if n not in supply_nodes:
            return pyo.Constraint.Skip
        outgoing = [(i, j) for (i, j) in m.E if i == n]
        if not outgoing:
            return pyo.Constraint.Feasible
        incoming = [(i, j) for (i, j) in m.E if j == n]
        supply_val = supply_dict.get(n, {}).get(k, 0)
        flow_out = sum(m.flow[i, j, k] for (i, j) in outgoing)
        flow_in  = sum(m.flow[i, j, k] for (i, j) in incoming) if incoming else 0
        # Supply nodes may also act as transshipment hubs:
        # net outflow (out - in) is limited by local supply.
        return flow_out - flow_in <= supply_val
    return supply_rule


def make_demand_rule(demand_nodes, demand_dict):
    def demand_rule(m, n, k):
        if n not in demand_nodes:
            return pyo.Constraint.Skip
        incoming = [(i, j) for (i, j) in m.E if j == n]
        demand_val = demand_dict.get(n, {}).get(k, 0)
        if not incoming:
            if demand_val == 0:
                return pyo.Constraint.Feasible
            return m.unmet[n, k] == demand_val
        inflow = sum(m.flow[i, j, k] for (i, j) in incoming)
        if demand_val == 0:
            return pyo.Constraint.Feasible
        return inflow + m.unmet[n, k] == demand_val
    return demand_rule


def make_balance_rule(supply_nodes, demand_nodes):
    def balance_rule(m, n, k):
        # Sources and sinks have their own rules.
        if n in supply_nodes or n in demand_nodes:
            return pyo.Constraint.Skip
        inflow  = sum(m.flow[i, j, k] for (i, j) in m.E if j == n)
        outflow = sum(m.flow[i, j, k] for (i, j) in m.E if i == n)
        return inflow == outflow
    return balance_rule


# ===============================
# PENALTY BASE
# ===============================

def compute_penalty_base(edges_df, supply_nodes, demand_nodes):
    """Cost of the most expensive supply->demand route (longest simple path).

    Shortage penalty = penalty base x lambda. Because the base is at least as large as
    any delivery route, the solver only leaves demand unmet when delivery is impossible.
    """
    G = nx.DiGraph()
    for _, r in edges_df.iterrows():
        G.add_edge(r["From"], r["To"], weight=float(r["Cost"]))

    longest = 0.0
    if nx.is_directed_acyclic_graph(G):
        order = list(nx.topological_sort(G))
        for s in supply_nodes:
            if s not in G:
                continue
            dist = {s: 0.0}
            for u in order:
                if u not in dist:
                    continue
                for v in G.successors(u):
                    cand = dist[u] + G[u][v]["weight"]
                    if cand > dist.get(v, -1.0):
                        dist[v] = cand
            for d in demand_nodes:
                if d in dist:
                    longest = max(longest, dist[d])
    else:
        # Network has cycles: any simple path has at most |N|-1 arcs
        costs = sorted((float(c) for c in edges_df["Cost"]), reverse=True)
        longest = sum(costs[:max(G.number_of_nodes() - 1, 1)])

    if longest <= 0:
        longest = float(edges_df["Cost"].max())
    return longest


# ===============================
# CORE SOLVE FUNCTION
# ===============================

def solve_mcnf(edges_df, commodities, supply_nodes, demand_nodes, supply_dict, demand_dict,
               penalty, cap_factor=1.0, uncapacitated=False):
    """Returns (model, edge_list, nodes) or (None, None, None).

    uncapacitated=True ignores all arc capacities (used to isolate the part of
    the shortage that is really caused by capacity).
    """
    nodes     = sorted(set(edges_df["From"]).union(set(edges_df["To"])))
    edge_list = list(zip(edges_df["From"], edges_df["To"]))

    model     = pyo.ConcreteModel()
    model.N   = pyo.Set(initialize=nodes)
    model.K   = pyo.Set(initialize=commodities)
    model.E   = pyo.Set(initialize=edge_list, dimen=2)

    cost_dict  = {(r["From"], r["To"]): float(r["Cost"]) for _, r in edges_df.iterrows()}
    model.cost = pyo.Param(model.E, initialize=cost_dict)

    if "Capacity" in edges_df.columns and not uncapacitated:
        cap_dict = {(r["From"], r["To"]): float(r["Capacity"]) * cap_factor
                    for _, r in edges_df.iterrows()}
        model.capacity = pyo.Param(model.E, initialize=cap_dict)
    else:
        model.capacity = pyo.Param(model.E, initialize=lambda m, i, j: 1e9)

    model.flow  = pyo.Var(model.E, model.K, domain=pyo.NonNegativeReals, initialize=0)
    model.unmet = pyo.Var(model.N, model.K, domain=pyo.NonNegativeReals, initialize=0)

    model.supply_con  = pyo.Constraint(model.N, model.K, rule=make_supply_rule(supply_nodes, supply_dict))
    model.demand_con  = pyo.Constraint(model.N, model.K, rule=make_demand_rule(demand_nodes, demand_dict))
    model.balance_con = pyo.Constraint(model.N, model.K, rule=make_balance_rule(supply_nodes, demand_nodes))

    def cap_rule(m, i, j):
        return sum(m.flow[i, j, k] for k in m.K) <= m.capacity[i, j]

    model.cap_con = pyo.Constraint(model.E, rule=cap_rule)

    model.obj = pyo.Objective(
        expr=(
            sum(model.cost[e] * model.flow[e, k] for e in model.E for k in model.K)
            + sum(penalty * model.unmet[n, k] for n in model.N for k in model.K)
        ),
        sense=pyo.minimize
    )

    solver = pyo.SolverFactory("glpk")
    result = solver.solve(model)

    if result.solver.termination_condition != pyo.TerminationCondition.optimal:
        return None, None, None

    return model, edge_list, nodes


# ===============================
# BASELINE (BAU): Heuristic cheapest-path shipping
# - Commodities are routed one at a time (first column gets first claim on
#   shared capacity: models un-coordinated "silo" shipping)
# - Largest demand node is served first, from its cheapest reachable farm
# - Respects shared edge capacities (edges are removed once saturated)
# - Not globally optimal (intentionally)
# ===============================

@dataclass
class HeuristicBaselineResult:
    N: list
    K: list
    E: list
    flow: dict
    unmet: dict


def solve_baseline_heuristic_bau(edges_df, commodities, supply_nodes, demand_nodes,
                                 supply_dict, demand_dict):
    nodes     = sorted(set(edges_df["From"]).union(set(edges_df["To"])))
    edge_list = list(zip(edges_df["From"], edges_df["To"]))

    cost = {(r["From"], r["To"]): float(r["Cost"]) for _, r in edges_df.iterrows()}
    if "Capacity" in edges_df.columns:
        cap = {(r["From"], r["To"]): float(r["Capacity"]) for _, r in edges_df.iterrows()}
    else:
        cap = defaultdict(lambda: 1e9)

    residual_cap = defaultdict(float, {e: cap[e] for e in edge_list})

    G = nx.DiGraph()
    for (i, j) in edge_list:
        G.add_edge(i, j, weight=cost.get((i, j), 0.0))

    flow  = defaultdict(float)   # key: (i, j, k)
    unmet = defaultdict(float)   # key: (n, k)

    supply_rem = {(n, k): float(supply_dict.get(n, {}).get(k, 0.0)) for n in supply_nodes for k in commodities}
    demand_rem = {(n, k): float(demand_dict.get(n, {}).get(k, 0.0)) for n in demand_nodes for k in commodities}

    for k in commodities:
        demand_order = sorted([n for n in demand_nodes if demand_rem.get((n, k), 0.0) > 1e-9],
                              key=lambda n: -demand_rem[(n, k)])

        for d in demand_order:
            while demand_rem[(d, k)] > 1e-9:
                best_s = None
                best_path = None
                best_cost = float("inf")

                for s in supply_nodes:
                    if supply_rem.get((s, k), 0.0) <= 1e-9:
                        continue
                    if not (G.has_node(s) and G.has_node(d)):
                        continue
                    try:
                        path = nx.shortest_path(G, source=s, target=d, weight="weight")
                        path_cost = nx.path_weight(G, path, weight="weight")
                    except (nx.NetworkXNoPath, nx.NodeNotFound):
                        continue

                    if path_cost < best_cost:
                        best_cost = path_cost
                        best_s = s
                        best_path = path

                if best_s is None or not best_path or len(best_path) < 2:
                    break

                path_edges = list(zip(best_path[:-1], best_path[1:]))
                if not path_edges:
                    break

                path_cap = min(residual_cap[e] for e in path_edges)
                if path_cap <= 1e-9:
                    saturated = [e for e in path_edges if residual_cap[e] <= 1e-9]
                    if saturated:
                        G.remove_edge(*saturated[0])
                    else:
                        break
                    continue

                x = min(supply_rem[(best_s, k)], demand_rem[(d, k)], path_cap)
                if x <= 1e-9:
                    break

                for e in path_edges:
                    flow[(e[0], e[1], k)] += x
                    residual_cap[e] -= x
                    if residual_cap[e] <= 1e-9 and G.has_edge(e[0], e[1]):
                        G.remove_edge(e[0], e[1])

                supply_rem[(best_s, k)] -= x
                demand_rem[(d, k)] -= x

    for d in demand_nodes:
        for k in commodities:
            rem = demand_rem.get((d, k), 0.0)
            unmet[(d, k)] = rem if rem > 1e-9 else 0.0

    return HeuristicBaselineResult(N=nodes, K=list(commodities), E=edge_list, flow=flow, unmet=unmet)


# ===============================
# METRIC HELPERS (work for both the Pyomo model and the heuristic result)
# ===============================

def make_flow_fn(mdl):
    return lambda i, j, k: safe_val(mdl.flow[i, j, k])


def net_outflow(flow_fn, edge_list, n, k):
    """Flow leaving node n minus flow entering it.

    For a farm that also relays other farms' goods (a hub), gross outflow
    double-counts the relayed goods; net outflow is what the farm itself ships.
    """
    out = sum(flow_fn(i, j, k) for (i, j) in edge_list if i == n)
    inn = sum(flow_fn(i, j, k) for (i, j) in edge_list if j == n)
    return out - inn


def total_unmet(mdl, demand_nodes, commodities, k_filter=None):
    ks = [k_filter] if k_filter else commodities
    return sum(safe_val(mdl.unmet[n, k]) for n in demand_nodes for k in ks)


def transport_cost(flow_fn, edge_list, commodities, cost_lookup):
    return sum(
        cost_lookup.get((i, j), 0.0) * flow_fn(i, j, k)
        for (i, j) in edge_list for k in commodities
    )


def farm_waste_rows(flow_fn, edge_list, supply_nodes, commodities, supply_dict):
    rows = []
    for n in sorted(supply_nodes):
        for k in commodities:
            supply_val = supply_dict.get(n, {}).get(k, 0)
            if supply_val == 0:
                continue
            shipped = net_outflow(flow_fn, edge_list, n, k)
            unused  = max(supply_val - shipped, 0)
            rows.append({
                "Farm Node":      n,
                "Commodity":      k,
                "Supply":         round(supply_val, 2),
                "Shipped (net)":  round(shipped, 2),
                "Unused (Waste)": round(unused, 2),
                "Waste %":        f"{round(unused / supply_val * 100, 2)}%",
            })
    return rows


def utilization_df(flow_fn, edge_list, commodities, cap_lookup):
    rows = []
    for (i, j) in edge_list:
        by_k = {k: flow_fn(i, j, k) for k in commodities}
        total = sum(by_k.values())
        cap  = cap_lookup[(i, j)]
        util = min(total / cap * 100, 100) if cap > 0 else 0
        status = "🔴 Critical" if util >= 90 else "🟡 Warning" if util >= 70 else "🟢 OK"
        row = {
            "From": i, "To": j,
            "Total Flow":    round(total, 2),
            "Capacity":      round(cap, 2),
            "Utilization %": round(util, 2),
            "Status":        status,
        }
        for k in commodities:
            row[f"{k} Flow"] = round(by_k[k], 2)
        rows.append(row)
    return pd.DataFrame(rows).sort_values("Utilization %", ascending=False)


# ===============================
# VISUALIZATION HELPERS
# ===============================

def draw_network(nodes, edge_list, flow_data, supply_nodes, demand_nodes, title="Network Flow"):
    G = nx.DiGraph()
    G.add_nodes_from(nodes)
    G.add_edges_from(edge_list)

    fig, ax = plt.subplots(figsize=(14, 8))

    warehouse_nodes = [n for n in G.nodes if n not in supply_nodes and n not in demand_nodes]

    pos = {}
    farms_list = sorted(supply_nodes)
    wh_list    = sorted(warehouse_nodes)
    mkt_list   = sorted(demand_nodes)

    for i, n in enumerate(farms_list):
        pos[n] = (0, i - len(farms_list) / 2)
    for i, n in enumerate(wh_list):
        pos[n] = (2, i - len(wh_list) / 2)
    for i, n in enumerate(mkt_list):
        pos[n] = (4, i - len(mkt_list) / 2)

    flow_map = {}
    for row in flow_data:
        key = (row[0], row[1])
        flow_map[key] = flow_map.get(key, 0) + row[3]

    active_edges   = [(i, j) for (i, j) in G.edges() if flow_map.get((i, j), 0) > 1e-6]
    inactive_edges = [(i, j) for (i, j) in G.edges() if flow_map.get((i, j), 0) <= 1e-6]
    max_flow       = max(flow_map.values()) if flow_map else 1

    nx.draw_networkx_nodes(G, pos, nodelist=list(supply_nodes),   node_color="#5DCAA5", node_size=500, ax=ax)
    nx.draw_networkx_nodes(G, pos, nodelist=warehouse_nodes,       node_color="#85B7EB", node_size=500, ax=ax)
    nx.draw_networkx_nodes(G, pos, nodelist=list(demand_nodes),    node_color="#F0997B", node_size=500, ax=ax)
    nx.draw_networkx_labels(G, pos, font_size=7, ax=ax)

    nx.draw_networkx_edges(G, pos, edgelist=inactive_edges,
                           edge_color="#cccccc", arrows=True, ax=ax,
                           arrowsize=10, width=0.5, connectionstyle="arc3,rad=0.1")

    if active_edges:
        edge_widths = [1 + 4 * flow_map.get(e, 0) / max_flow for e in active_edges]
        nx.draw_networkx_edges(G, pos, edgelist=active_edges,
                               edge_color="#534AB7", arrows=True, ax=ax,
                               width=edge_widths, arrowsize=15,
                               connectionstyle="arc3,rad=0.1")

    patches = [
        mpatches.Patch(color="#5DCAA5", label="Farm (Supply)"),
        mpatches.Patch(color="#85B7EB", label="Warehouse"),
        mpatches.Patch(color="#F0997B", label="Market (Demand)"),
        mpatches.Patch(color="#534AB7", label="Active Flow"),
        mpatches.Patch(color="#cccccc", label="Unused Route"),
    ]
    ax.legend(handles=patches, loc="upper right", fontsize=8)
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.axis("off")
    plt.tight_layout()
    return fig


def draw_waste_bar(waste_df, commodities):
    if waste_df.empty:
        return None
    fig, axes = plt.subplots(1, len(commodities), figsize=(6 * len(commodities), 5))
    if len(commodities) == 1:
        axes = [axes]

    for ax, k in zip(axes, commodities):
        kdf = waste_df[waste_df["Commodity"] == k].copy()
        kdf = kdf[kdf["Unused (Waste)"] > 0].sort_values("Unused (Waste)", ascending=False)
        if kdf.empty:
            ax.set_title(f"{k} — No Unused Supply")
            continue
        colors = ["#E24B4A" if w > kdf["Unused (Waste)"].mean() else "#EF9F27" for w in kdf["Unused (Waste)"]]
        ax.barh(kdf["Farm Node"], kdf["Unused (Waste)"], color=colors)
        ax.set_xlabel("Unused Supply (net, tons)")
        ax.set_title(f"{k} — Unused Supply per Farm")
        ax.invert_yaxis()

    plt.tight_layout()
    return fig


def draw_bottleneck_chart(util_df):
    top = util_df.head(20)
    if top.empty:
        return None

    labels = [f"{r['From']}→{r['To']}" for _, r in top.iterrows()]
    values = list(top["Utilization %"])
    colors = ["#E24B4A" if v >= 90 else "#EF9F27" if v >= 70 else "#639922" for v in values]

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.barh(labels[::-1], values[::-1], color=colors[::-1])
    ax.axvline(x=90, color="#E24B4A", linestyle="--", linewidth=1, label="Critical (≥90%)")
    ax.axvline(x=70, color="#EF9F27", linestyle="--", linewidth=1, label="Warning (≥70%)")
    ax.set_xlabel("Edge Utilization %")
    ax.set_title("Top 20 Most Utilized Edges (Bottleneck Analysis)")
    ax.legend(fontsize=8)
    plt.tight_layout()
    return fig


def fig_to_bytes(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    buf.seek(0)
    return buf


def fmt_num(x):
    if x != x:  # NaN
        return "—"
    return f"{x:,.2f}"


def fmt_pct_delta(base, opt):
    if base != base or base == 0:
        return "—"
    return f"{((opt - base) / base) * 100:,.2f}%"


def flow_rows(mdl, flow_fn):
    rows = []
    for (i, j) in mdl.E:
        for k in mdl.K:
            val = flow_fn(i, j, k)
            if val > 1e-6:
                rows.append([i, j, k, round(val, 4)])
    return rows


def unmet_rows(mdl, demand_nodes, commodities):
    rows = []
    for n in sorted(demand_nodes):
        for k in commodities:
            val = safe_val(mdl.unmet[n, k])
            if val > 1e-6:
                rows.append([n, k, round(val, 4)])
    return rows


# ===============================
# MAIN LOGIC
# ===============================
if supply_file and demand_file and edges_file:

    supply_df_raw = pd.read_csv(supply_file)
    demand_df_raw = pd.read_csv(demand_file)
    edges_df_raw  = pd.read_csv(edges_file)

    # Clean stray whitespace in headers and node names
    for _df in (supply_df_raw, demand_df_raw, edges_df_raw):
        _df.columns = [str(c).strip() for c in _df.columns]
    for _df in (supply_df_raw, demand_df_raw):
        _df["Node"] = _df["Node"].astype(str).str.strip()
    edges_df_raw["From"] = edges_df_raw["From"].astype(str).str.strip()
    edges_df_raw["To"]   = edges_df_raw["To"].astype(str).str.strip()

    st.subheader("📊 Input Data")
    with st.expander("View raw data"):
        st.write("Supply", supply_df_raw)
        st.write("Demand", demand_df_raw)
        st.write("Edges",  edges_df_raw)

    supply_df = supply_df_raw.copy()
    demand_df = demand_df_raw.copy()
    edges_df  = edges_df_raw.copy()

    commodities = list(supply_df.columns[1:])

    # Scenario multipliers
    supply_df[commodities] = supply_df[commodities].astype(float) * float(supply_multiplier)
    demand_df[commodities] = demand_df[commodities].astype(float) * float(demand_multiplier)
    if "Capacity" in edges_df.columns:
        edges_df["Capacity"] = edges_df["Capacity"].astype(float) * float(capacity_multiplier)

    nodes        = sorted(set(edges_df["From"]).union(set(edges_df["To"])))
    supply_nodes = set(supply_df["Node"])
    demand_nodes = set(demand_df["Node"])
    supply_dict  = supply_df.set_index("Node").to_dict(orient="index")
    demand_dict  = demand_df.set_index("Node").to_dict(orient="index")

    # Sanity checks on the input network
    missing = [n for n in (supply_nodes | demand_nodes) if n not in nodes]
    if missing:
        st.warning(f"These supply/demand nodes do not appear in the edges file: {missing}")
    out_of_demand = [(i, j) for (i, j) in zip(edges_df["From"], edges_df["To"]) if i in demand_nodes]
    if out_of_demand:
        st.warning("Edges leave demand nodes, which can create flow from nowhere "
                   f"under the current demand constraint: {out_of_demand}")

    if "Capacity" in edges_df.columns:
        cap_lookup = {(r["From"], r["To"]): float(r["Capacity"]) for _, r in edges_df.iterrows()}
    else:
        cap_lookup = defaultdict(lambda: 1e9)
    cost_lookup = {(r["From"], r["To"]): float(r["Cost"]) for _, r in edges_df.iterrows()}

    penalty_base = compute_penalty_base(edges_df, supply_nodes, demand_nodes)
    penalty      = penalty_base * penalty_multiplier

    def run_mcnf(pen=penalty, cap_factor=1.0, uncapacitated=False):
        return solve_mcnf(edges_df, commodities, supply_nodes, demand_nodes, supply_dict, demand_dict,
                          pen, cap_factor=cap_factor, uncapacitated=uncapacitated)

    # ===============================
    # SOLVE OPTIMIZED MODEL
    # ===============================
    with st.spinner("Solving optimized model..."):
        model, edge_list, nodes = run_mcnf()

    if model is None:
        st.error("No optimal solution found.")
        st.stop()

    st.success("✅ Optimization Successful")
    opt_flow = make_flow_fn(model)

    # ===============================
    # SOLVE UNCAPACITATED MODEL (isolates capacity-caused shortage)
    # ===============================
    with st.spinner("Solving uncapacitated reference model..."):
        unc_model, _, _ = run_mcnf(uncapacitated=True)
    unc_ok = unc_model is not None

    # ===============================
    # SOLVE BASELINE (heuristic / BAU)
    # ===============================
    with st.spinner("Solving baseline model..."):
        baseline_model = solve_baseline_heuristic_bau(
            edges_df, commodities, supply_nodes, demand_nodes, supply_dict, demand_dict
        )
    base_flow = make_flow_fn(baseline_model)

    # ===============================
    # FLOW RESULTS
    # ===============================
    flow_data = flow_rows(model, opt_flow)
    flow_df = pd.DataFrame(flow_data, columns=["From", "To", "Commodity", "Flow"])

    st.subheader("📦 Optimal Flows")
    st.dataframe(flow_df)

    baseline_flow_df = pd.DataFrame(flow_rows(baseline_model, base_flow),
                                    columns=["From", "To", "Commodity", "Flow"])
    with st.expander("📦 Baseline Flows (Heuristic / BAU Baseline)", expanded=False):
        st.dataframe(baseline_flow_df, width="stretch")

    # ===============================
    # UNMET DEMAND
    # ===============================
    unmet_data = unmet_rows(model, demand_nodes, commodities)
    if unmet_data:
        st.subheader("⚠️ Unmet Demand (Market Shortage)")
        st.dataframe(pd.DataFrame(unmet_data, columns=["Node", "Commodity", "Unmet Demand"]))
    else:
        st.info("✅ All demand is fully met.")

    baseline_unmet_data = unmet_rows(baseline_model, demand_nodes, commodities)
    with st.expander("⚠️ Baseline Unmet Demand (Heuristic / BAU Baseline)", expanded=False):
        if baseline_unmet_data:
            st.dataframe(pd.DataFrame(baseline_unmet_data, columns=["Node", "Commodity", "Unmet Demand"]),
                         width="stretch")
        else:
            st.info("All demand is fully met in the baseline model.")

    # ===============================
    # WASTE ANALYSIS
    # ===============================
    st.subheader("🗑️ Supply Waste Analysis (Obj. 3)")

    total_supply_all = float(sum(supply_df[k].sum() for k in commodities))
    total_demand_all = float(sum(demand_df[k].sum() for k in commodities))

    opt_unmet_units  = total_unmet(model, demand_nodes, commodities)
    unc_unmet_units  = total_unmet(unc_model, demand_nodes, commodities) if unc_ok else float("nan")
    base_unmet_units = total_unmet(baseline_model, demand_nodes, commodities)

    structural_surplus  = max(total_supply_all - total_demand_all, 0)
    structural_shortage = unc_unmet_units                                   # shortage even with infinite capacity
    capacity_caused     = max(opt_unmet_units - unc_unmet_units, 0) if unc_ok else float("nan")
    stranded_total      = total_supply_all - (total_demand_all - opt_unmet_units)
    net_shipped_total   = sum(net_outflow(opt_flow, edge_list, n, k) for n in supply_nodes for k in commodities)
    utilization         = net_shipped_total / total_supply_all * 100 if total_supply_all > 0 else 0

    st.caption(
        "Definitions — Structural surplus: supply that exceeds total demand (nobody asked for it). "
        "Structural shortage: demand that stays unmet even with unlimited arc capacity (local supply deficit / "
        "missing routes). Capacity-caused shortage: extra unmet demand that appears only because arcs have "
        "limited capacity. Only the last is 'logistical waste' in the sense of this study."
    )

    def pct_supply(x):
        return x / total_supply_all * 100 if total_supply_all > 0 else 0.0

    wc1, wc2, wc3, wc4 = st.columns(4)
    wc1.metric("Structural Surplus",
               f"{structural_surplus:,.0f}  ({pct_supply(structural_surplus):.2f}% of supply)")
    wc2.metric("Structural Shortage (no capacity limits)",
               f"{structural_shortage:,.0f}" if unc_ok else "—")
    wc3.metric("Capacity-Caused Shortage",
               f"{capacity_caused:,.0f}  ({pct_supply(capacity_caused):.2f}% of supply)" if unc_ok else "—",
               help="Unmet demand (capacitated) − unmet demand (uncapacitated)")
    wc4.metric("Net Supply Utilization", f"{utilization:.2f}%",
               help="Net farm outflow (out − in) ÷ total supply. Relayed goods are not double-counted.")

    # Per-farm table (net shipped)
    waste_df = pd.DataFrame(farm_waste_rows(opt_flow, edge_list, supply_nodes, commodities, supply_dict))
    st.markdown("#### Unused Supply per Farm (net)")
    st.dataframe(waste_df)

    baseline_waste_df = pd.DataFrame(
        farm_waste_rows(base_flow, edge_list, supply_nodes, commodities, supply_dict)
    )
    with st.expander("🗑️ Baseline Unused Supply per Farm (Heuristic / BAU Baseline)", expanded=False):
        st.dataframe(baseline_waste_df, width="stretch")

    # Per-commodity summary
    st.markdown("#### Waste Summary by Commodity")
    summary_rows = []
    for k in commodities:
        ts_k = float(supply_df[k].sum())
        td_k = float(demand_df[k].sum())
        um_k = total_unmet(model, demand_nodes, commodities, k)
        um_unc_k = total_unmet(unc_model, demand_nodes, commodities, k) if unc_ok else float("nan")
        summary_rows.append({
            "Commodity":                  k,
            "Total Supply":               round(ts_k, 2),
            "Total Demand":               round(td_k, 2),
            "Structural Surplus":         round(max(ts_k - td_k, 0), 2),
            "Unmet (Optimized)":          round(um_k, 2),
            "Unmet (No Capacity Limits)": round(um_unc_k, 2) if unc_ok else None,
            "Capacity-Caused Shortage":   round(max(um_k - um_unc_k, 0), 2) if unc_ok else None,
            "Stranded Supply":            round(ts_k - (td_k - um_k), 2),
        })
    summary_df = pd.DataFrame(summary_rows)
    st.dataframe(summary_df)

    # ===============================
    # BOTTLENECK ANALYSIS
    # ===============================
    st.subheader("🔍 Network Bottleneck Analysis")

    bottleneck_df = utilization_df(opt_flow, edge_list, commodities, cap_lookup)

    bc1, bc2, bc3 = st.columns(3)
    bc1.metric("🔴 Critical Edges (≥90% full)", int((bottleneck_df["Status"] == "🔴 Critical").sum()))
    bc2.metric("🟡 Warning Edges (70–90% full)", int((bottleneck_df["Status"] == "🟡 Warning").sum()))
    bc3.metric("🟢 OK Edges (<70% full)",        int((bottleneck_df["Status"] == "🟢 OK").sum()))

    st.dataframe(bottleneck_df)

    baseline_bottleneck_df = utilization_df(base_flow, edge_list, commodities, cap_lookup)
    with st.expander("🔍 Baseline Bottleneck Analysis (Heuristic / BAU Baseline)", expanded=False):
        bc1b, bc2b = st.columns(2)
        bc1b.metric("🔴 Critical Edges (≥90% full)", int((baseline_bottleneck_df["Status"] == "🔴 Critical").sum()))
        bc2b.metric("🟡 Warning Edges (70–90% full)", int((baseline_bottleneck_df["Status"] == "🟡 Warning").sum()))
        st.dataframe(baseline_bottleneck_df, width="stretch")

    # ===============================
    # VISUALIZATIONS
    # ===============================
    st.subheader("📊 Visualizations")

    tab1, tab2, tab3 = st.tabs(["🌐 Flow Network", "🗑️ Unused Supply per Farm", "🔥 Bottleneck Chart"])

    with tab1:
        fig = draw_network(nodes, edge_list, flow_data, supply_nodes, demand_nodes, "Optimized Flow Network")
        st.pyplot(fig)
        st.download_button("⬇️ Download Network Chart",
                           fig_to_bytes(fig), file_name="mcnf_network.png", mime="image/png")
        plt.close(fig)

    with tab2:
        fig2 = draw_waste_bar(waste_df, commodities)
        if fig2:
            st.pyplot(fig2)
            st.download_button("⬇️ Download Waste Chart",
                               fig_to_bytes(fig2), file_name="mcnf_waste_chart.png", mime="image/png")
            plt.close(fig2)
        else:
            st.info("No supply data to display.")

    with tab3:
        fig3 = draw_bottleneck_chart(bottleneck_df)
        if fig3:
            st.pyplot(fig3)
            st.download_button("⬇️ Download Bottleneck Chart",
                               fig_to_bytes(fig3), file_name="mcnf_bottleneck.png", mime="image/png")
            plt.close(fig3)
        else:
            st.info("No bottleneck data to display.")

    # ===============================
    # COST METRICS
    # ===============================
    opt_transport_cost = transport_cost(opt_flow, edge_list, commodities, cost_lookup)
    opt_penalty_cost   = opt_unmet_units * penalty
    opt_total_cost     = opt_transport_cost + opt_penalty_cost
    opt_delivered      = total_demand_all - opt_unmet_units

    base_transport_cost = transport_cost(base_flow, edge_list, commodities, cost_lookup)
    base_penalty_cost   = base_unmet_units * penalty
    base_total_cost     = base_transport_cost + base_penalty_cost
    base_delivered      = total_demand_all - base_unmet_units

    demand_satisfaction_rate = opt_delivered / total_demand_all * 100 if total_demand_all > 0 else 0
    base_demand_satisfaction_rate = base_delivered / total_demand_all * 100 if total_demand_all > 0 else 0
    base_utilization = base_delivered / total_supply_all * 100 if total_supply_all > 0 else 0

    savings = base_total_cost - opt_total_cost
    improvement_pct = savings / base_total_cost * 100 if base_total_cost > 0 else 0

    transport_saving = base_transport_cost - opt_transport_cost
    penalty_saving   = base_penalty_cost - opt_penalty_cost
    transport_saving_pct = transport_saving / base_transport_cost * 100 if base_transport_cost > 0 else 0

    st.subheader("📈 Cost Performance Metrics")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Baseline Cost",        fmt_num(base_total_cost))
    c2.metric("Optimized Cost",       fmt_num(opt_total_cost))
    c3.metric("Savings",              fmt_num(savings))
    c4.metric("Improvement %",        f"{improvement_pct:.2f}%")
    c5.metric("Net Supply Utilization %", f"{utilization:.2f}%",
              help="Net farm outflow ÷ total supply (relayed goods not double-counted)")
    c6.metric("Demand Satisfaction Rate %", f"{demand_satisfaction_rate:.2f}%",
              help="Demand delivered ÷ total demand")

    st.markdown("#### Where the savings come from")
    s1, s2, s3 = st.columns(3)
    s1.metric("Transport-cost saving", fmt_num(transport_saving), f"{transport_saving_pct:.2f}% of BAU transport")
    s2.metric("Shortage-penalty saving", fmt_num(penalty_saving),
              help="BAU leaves more demand unserved and pays the shortage penalty on it")
    s3.metric("Share of total saving from fewer shortages",
              f"{(penalty_saving / savings * 100):.1f}%" if savings > 0 else "—")

    if unc_ok:
        unc_flow = make_flow_fn(unc_model)
        unc_total = (transport_cost(unc_flow, edge_list, commodities, cost_lookup)
                     + unc_unmet_units * penalty)
        capacity_cost = opt_total_cost - unc_total
        st.metric("Cost of capacity limits (capacitated − uncapacitated optimum)",
                  fmt_num(capacity_cost),
                  f"{capacity_cost / unc_total * 100:.2f}% above the unconstrained optimum" if unc_total > 0 else None)

    st.write("🟡 Total Shortage Penalty:", round(opt_penalty_cost, 4))

    comparison_rows = [
        ("Total cost", base_total_cost, opt_total_cost),
        ("Transport cost", base_transport_cost, opt_transport_cost),
        ("Penalty cost", base_penalty_cost, opt_penalty_cost),
        ("Unmet demand (units)", base_unmet_units, opt_unmet_units),
        ("Delivered to markets (units)", base_delivered, opt_delivered),
        ("Demand satisfaction rate (%)", base_demand_satisfaction_rate, demand_satisfaction_rate),
        ("Net supply utilization (%)", base_utilization, utilization),
        ("Stranded supply (units)", total_supply_all - base_delivered, stranded_total),
    ]
    comparison_df = pd.DataFrame([
        {
            "Metric": name,
            "Baseline": fmt_num(b),
            "Optimized": fmt_num(o),
            "Δ (Opt − Base)": fmt_num(o - b),
            "Δ% (Opt vs Base)": fmt_pct_delta(b, o),
        }
        for name, b, o in comparison_rows
    ])

    st.subheader("🔁 Baseline vs Optimized Comparison")
    st.dataframe(comparison_df, hide_index=True, width="stretch")

    # ===============================
    # SENSITIVITY ANALYSIS
    # ===============================
    st.subheader("🧪 Sensitivity Analysis")
    if st.checkbox("Run capacity-scale and penalty sensitivity (solves several LPs)"):
        if "Capacity" not in edges_df.columns:
            st.info("The edges file has no Capacity column, so a capacity sweep is not meaningful.")
        else:
            cap_rows = []
            with st.spinner("Running capacity sweep..."):
                for cf in [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4]:
                    m_s, el_s, _ = run_mcnf(cap_factor=cf)
                    if m_s is None:
                        continue
                    f_s   = make_flow_fn(m_s)
                    tc_s  = transport_cost(f_s, el_s, commodities, cost_lookup)
                    um_s  = total_unmet(m_s, demand_nodes, commodities)
                    tot_s = tc_s + um_s * penalty

                    edges_s = edges_df.copy()
                    edges_s["Capacity"] = edges_s["Capacity"].astype(float) * cf
                    b_s = solve_baseline_heuristic_bau(
                        edges_s, commodities, supply_nodes, demand_nodes, supply_dict, demand_dict
                    )
                    bf_s   = make_flow_fn(b_s)
                    btc_s  = transport_cost(bf_s, el_s, commodities, cost_lookup)
                    bum_s  = total_unmet(b_s, demand_nodes, commodities)
                    btot_s = btc_s + bum_s * penalty

                    cap_rows.append({
                        "Capacity scale":  cf,
                        "MCNF total cost": round(tot_s, 2),
                        "MCNF unmet %":    round(um_s / total_demand_all * 100, 2),
                        "BAU total cost":  round(btot_s, 2),
                        "BAU unmet %":     round(bum_s / total_demand_all * 100, 2),
                        "Gap (BAU−MCNF) %": round((btot_s - tot_s) / btot_s * 100, 2) if btot_s > 0 else 0,
                    })
            st.markdown("#### Capacity sweep (relative to the current Capacity Multiplier)")
            st.dataframe(pd.DataFrame(cap_rows), hide_index=True)

        pen_rows = []
        with st.spinner("Running penalty sweep..."):
            for lam in [1.1, 1.25, 1.5, 2.0, 3.0]:
                m_p, _, _ = run_mcnf(pen=penalty_base * lam)
                if m_p is None:
                    continue
                um_p = total_unmet(m_p, demand_nodes, commodities)
                pen_rows.append({
                    "λ": lam,
                    "Penalty per unit": round(penalty_base * lam, 2),
                    "Unmet demand (units)": round(um_p, 2),
                    "Unmet %": round(um_p / total_demand_all * 100, 2),
                })
        st.markdown("#### Penalty sweep")
        st.dataframe(pd.DataFrame(pen_rows), hide_index=True)
        st.caption("If unmet demand keeps falling as λ rises, the penalty at lower λ is too weak and the solver is "
                   "choosing shortage over delivery. Report results at a λ where the curve has flattened.")

    # ===============================
    # DOWNLOADS
    # ===============================
    st.subheader("⬇️ Export All Results")
    d1, d2, d3, d4, d5 = st.columns(5)
    with d1:
        st.download_button("Optimal Flows",      flow_df.to_csv(index=False),       file_name="mcnf_flows.csv")
    with d2:
        st.download_button("Farm Unused Supply", waste_df.to_csv(index=False),      file_name="mcnf_waste.csv")
    with d3:
        st.download_button("Waste Summary",      summary_df.to_csv(index=False),    file_name="mcnf_waste_summary.csv")
    with d4:
        st.download_button("Bottleneck Report",  bottleneck_df.to_csv(index=False), file_name="mcnf_bottlenecks.csv")
    with d5:
        st.download_button("Comparison",         comparison_df.to_csv(index=False), file_name="mcnf_comparison.csv")

    # ===============================
    # MODEL SUMMARY
    # ===============================
    st.subheader("📘 Model Summary")
    st.write(f"Nodes: {len(nodes)}")
    st.write(f"Edges: {len(edge_list)}")
    st.write(f"Commodities: {len(commodities)}")
    st.write(f"Penalty base (most expensive supply→demand route cost): {round(penalty_base, 4)}")
    st.write(f"Shortage penalty per unit: {round(penalty, 4)} (= {round(penalty_base, 4)} × {penalty_multiplier})")
