CUTINES, ADRIAN CARL B.
2021-07168


README.txt - SP Project: Optimizing Agricultural Supply Chain Distribution
Using Multi-Commodity Network Flow Model
=====================================================================

Optimizes the distribution of palay (milled rice equivalent) and corn
across Regions I, II, III and Metro Manila using a Multi-Commodity Network
Flow (MCNF) model (Pyomo + GLPK), compares it with a greedy Business-as-Usual
(BAU) baseline, and shows the results in a Streamlit web app.


---------------------------------------------------------------------
1. Project files
---------------------------------------------------------------------
  app.py        Streamlit application (the optimizer and dashboard)
  supply2.csv   Supply per province (t/yr)        - case study input
  demand2.csv   Demand per province + Metro (t/yr) - case study input
  edges2.csv    Arcs: cost per ton and capacity    - case study input

  Optional small test network (Illustrative Example in the paper):
  supply.csv, demand.csv, edges.csv


---------------------------------------------------------------------
2. Installation (Windows)
---------------------------------------------------------------------
The app needs Python, five Python packages, and the GLPK solver, all in
the SAME environment. Option A is the easiest because it installs GLPK
for you.

Option A (Recommended): Miniconda
---------------------------------
1. Download and install Miniconda (default settings):
       https://docs.conda.io/en/latest/miniconda.html
2. Open "Anaconda Prompt" and run:
       conda create -n pyomo_env python=3.11
       conda activate pyomo_env
       conda install -c conda-forge pyomo glpk streamlit pandas networkx matplotlib
3. Check that everything is installed:
       python -c "import pyomo.environ, streamlit, pandas, networkx, matplotlib; print('OK')"
       glpsol --version

Every time you open a new Anaconda Prompt, run
       conda activate pyomo_env
before starting the app.

Option B: Plain Python + manual GLPK
------------------------------------
1. Install Python 3.11 or 3.12 from https://www.python.org/downloads/
   IMPORTANT: tick "Add Python to PATH" before clicking Install.
   (Very new Python versions may not yet be supported by every package.)
2. Open PowerShell and install the packages:
       python -m pip install --upgrade pip
       python -m pip install streamlit pyomo pandas networkx matplotlib
3. Install GLPK:
   a. Download the Windows binaries: https://sourceforge.net/projects/winglpk/
   b. Extract them, e.g. to C:\glpk
   c. Add the "w64" folder inside it (e.g. C:\glpk\glpk-4.65\w64) to your
      PATH (Start > "Edit the system environment variables" >
      Environment Variables > Path > Edit > New).
   d. Close and reopen PowerShell, then test:
       glpsol --version


---------------------------------------------------------------------
3. Running the app
---------------------------------------------------------------------
1. Go to the project folder, for example:
       cd C:\path\to\SP
2. Start the app:
       python -m streamlit run app.py
3. Your browser opens at http://localhost:8501
   (if it does not, open that address manually).

Do NOT run "python app.py" directly; that starts Streamlit in bare mode
with no web page.


---------------------------------------------------------------------
4. Using the app
---------------------------------------------------------------------
1. Upload the three CSV files: Supply, Demand, Edges.
2. Sidebar settings:
   - Penalty Multiplier (lambda): shortage cost per ton =
     (most expensive supply-to-demand route cost) x lambda.
     Default 2.0 (= 2,040 x 2.0 = 4,080 per ton for the case study).
   - Supply / Demand / Capacity Multiplier: scale all inputs to test
     scenarios (e.g. Capacity 0.7 = 30% of road capacity lost).
     Leave all at 1.0 to reproduce the paper.
3. The app shows, in order: optimal flows, unmet demand, waste analysis,
   bottleneck analysis, charts, cost metrics and the BAU vs. optimized
   comparison. BAU results are in the collapsible "Baseline" sections.
4. Sensitivity analysis: tick "Run capacity-scale and penalty
   sensitivity" to solve the capacity sweep (100% to 40%) and the
   penalty sweep (lambda 1.1 to 3.0). This takes longer.
5. Downloads:
   - Charts: "Download Network/Waste/Bottleneck Chart" in each tab
     (mcnf_network.png is Figure 4 in the paper).
   - Tables: "Export All Results" section (flows, unused supply, waste
     summary, bottlenecks, comparison) as CSV.


---------------------------------------------------------------------
5. Input file format
---------------------------------------------------------------------
supply CSV:  Node,Palay,Corn            (tons per year; first column = Node)
demand CSV:  Node,Palay,Corn            (same commodity columns as supply)
edges CSV:   From,To,Cost,Capacity      (cost per ton; capacity in t/yr)

- Commodity columns can have any names, but supply and demand must use
  the same ones. The first commodity column is routed first by BAU.
- Supply node names end in _S, demand nodes in _D. Any other node in
  edges.csv (e.g. a warehouse ending in _W) is treated as a pass-through
  node: what goes in must come out.
- Every supply and demand node must appear in the edges file.


---------------------------------------------------------------------
6. Checking your installation (expected results)
---------------------------------------------------------------------
With supply2.csv, demand2.csv, edges2.csv, lambda = 2.0 and all
multipliers at 1.0, the comparison table should show:

  Total cost        BAU 3,089,322,212.02   MCNF 1,986,928,946.10  (-35.68%)
  Transport cost    BAU 1,699,575,231.22   MCNF 1,661,644,540.50  (-2.23%)
  Unmet demand      BAU 340,624.26 t       MCNF 79,726.57 t
  Demand satisfied  BAU 93.21%             MCNF 98.41%

With the small test network (supply.csv, demand.csv, edges.csv):
  Transport cost    BAU 550                MCNF 150

If your numbers match, the installation is correct.


---------------------------------------------------------------------
7. Troubleshooting
---------------------------------------------------------------------
"No executable found for solver 'glpk'" or "No optimal solution found"
    GLPK is not on PATH or not in the active environment. Run
    glpsol --version. With Miniconda, run conda activate pyomo_env first.

"ModuleNotFoundError: No module named ..."
    Install the missing package in the SAME environment:
        python -m pip install <package>
    (or: conda install -c conda-forge <package>)

Error mentioning width="stretch"
    Your Streamlit is too old. Upgrade it:
        python -m pip install --upgrade streamlit

"streamlit is not recognized as a command"
    Use: python -m streamlit run app.py

Warning "nodes do not appear in the edges file"
    A node in the supply/demand CSV is misspelled or missing from the
    edges CSV. Check names, including the _S / _D suffixes.
