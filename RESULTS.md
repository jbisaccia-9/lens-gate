# Results

Generated 2026-10-17 by `scripts/make_results.py` - every block below is captured command output, not prose.

## Unit tests

`python -m pytest -q` - exit 0, OK

```
........................................................................ [ 93%]
.....                                                                    [100%]
77 passed in 5.66s
```

## Seed the synthetic world

`python -m lensgate seed data` - exit 0, OK

```
seeded Larkmoor Goods (fictional): 12 stores, 27 weeks, 6 closed months
  rows: ref.stores 12, sales.weekly_sales 1620, ops.inventory_weekly 1620, people.staff_hours_weekly 972, finance.store_pnl_monthly 72
  directory: 6 people; planted drift: jordan.pike still in Metabase 'Finance'
```

## The seeded morning: five questions, four people, the real broker and gate

`python -m lensgate demo data --out out/run` - exit 0, OK

```
PASS  20261012T090500-07bf3cf3  avery.lin     delivered  (3 call(s) ran, 0 refused)
      Q: How has weekly revenue by category trended over the last 8 weeks? Save it as a chart in
      my sandbox.
      A: Apparel led the last 8 weeks with 1,794,919.64 in revenue (+8.6% vs the 8 weeks
      before). Accessories was smallest at 723,596.10 (-0.2%). The biggest swing was Outdoor at
      -17.2%. I saved the chart to Store Floor / Sandbox.
PASS  20261012T092000-ec470205  marisol.tate  delivered  (1 call(s) ran, 0 refused)
      Q: Which stores had the most stockout days in September?
      A: Most stockout days in September: Copper Ridge (Highlands) with 47, then Saltmarsh
      (Coastal) with 43, then Stonegate (Highlands) with 15.
PASS  20261012T100200-dc561455  devin.okafor  delivered  (3 call(s) ran, 0 refused)
      Q: What was operating margin by region in Q3? Add it to the Finance Monthly dashboard.
      A: Q3 operating margin by region: Coastal 15.5% on 3,253,524.32 revenue; Valley 14.9% on
      3,386,892.46 revenue; Highlands 13.9% on 2,845,194.57 revenue. It's now on Finance
      Monthly.
PASS  20261012T103000-e8992b82  priya.raman   delivered  (3 call(s) ran, 0 refused)
      Q: Put a monthly revenue vs labor cost chart on the Executive Overview, and tell me how
      labor as a share of revenue moved from April to September.
      A: Labor ran 15.4% of revenue in 2026-04 and 15.2% in 2026-09 (labor cost 430,446.41 vs
      441,106.89). It's now on the Executive Overview.
PASS  20261012T111500-50a4a761  avery.lin     delivered, 1 call(s) refused  (0 call(s) ran, 1 refused)
      Q: What was operating income by store last quarter?
      refused: L3: connection wh_finance is not granted to store_lead (has ['wh_ops',
      'wh_sales'])
      A: I can't answer that: it needs finance data, and your role doesn't include it. If you
      think it should, ask an admin to review your access.

5 turns: 4 answered in full, 1 declined in scope (the out-of-scope query never ran), 0 withheld. 5 Metabase write(s), each as the person who asked:
  save_question     Weekly revenue by category, last 8 weeks  ->  Store Floor/Sandbox  (by avery.lin)
  save_question     Q3 operating margin by region  ->  Finance  (by devin.okafor)
  add_to_dashboard  Q3 operating margin by region  ->  Finance Monthly  (by devin.okafor)
  save_question     Monthly revenue vs labor cost  ->  Executive  (by priya.raman)
  add_to_dashboard  Monthly revenue vs labor cost  ->  Executive Overview  (by priya.raman)
```

## Re-grade the morning from disk

`python -m lensgate check-dir out/run --data data` - exit 0, OK

```
PASS  20261012T090500-07bf3cf3
PASS  20261012T092000-ec470205
PASS  20261012T100200-dc561455
PASS  20261012T103000-e8992b82
PASS  20261012T111500-50a4a761

5/5 passed
```

## Directory vs Metabase: the planted drift must be reported

`python -m lensgate sync data` - expected non-zero exit, OK

```
drift  jordan.pike@larkmoor.example: stale Finance (directory no longer grants it)
1 difference(s) between the directory and Metabase
```

## Build the counterexamples

`python -m lensgate counterexamples out/cx --data data` - exit 0, OK

```
l1-unmapped-group            a contractor's token verifies, but their only group maps to no role; the broker answered anyway
l2-shared-key                the stockout question is in scope, but it ran under the broker's admin API key 'to save a round trip'
l3-cross-domain-read         a store lead's query on wh_sales joins finance.store_pnl_monthly; the answer ranks stores without a single number
l4-write-outside-collection  finance saved its margin chart into the Executive collection and pinned it to the Executive Overview
l5-invented-number           correct query, correct figures - plus one friendly projection nobody computed
l6-stale-membership          Jordan moved from Finance to store lead in the directory; Metabase still has them in Finance
```

## Gate refuses every counterexample, each for its own rule

`python -m lensgate check-dir out/cx --data data` - expected non-zero exit, OK

```
FAIL  l1-unmapped-group  (own rule)
      L1 identity: none of the token's 1 group(s) maps to a role
      not evaluated without a verified identity: L2, L3, L4, L5, L6
FAIL  l2-shared-key  (own rule)
      L2 credential: work ran under credential kind 'api-key' (svc-lens-broker), not the
      person's own session
      L2 credential: svc-lens-broker is a service principal
FAIL  l3-cross-domain-read  (own rule)
      L3 read scope: call 1: finance.store_pnl_monthly: schema 'finance' is outside ['ref',
      'sales']
FAIL  l4-write-outside-collection  (own rule)
      L4 write scope: call 2: collection 'Executive' is not curatable by finance (can curate
      ['Finance'])
      L4 write scope: call 3: dashboard 'Executive Overview' lives in 'Executive', not curatable
      by finance
FAIL  l5-invented-number  (own rule)
      L5 numbers: answer contains number(s) no verified result supplied: 16.85%
FAIL  l6-stale-membership  (own rule)
      L6 sync: Metabase still grants ['Finance'] that the directory no longer does

0/6 passed
```
