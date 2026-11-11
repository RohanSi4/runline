# RunLine presentation script — November 11

Allow about five minutes. Open https://runline-nine.vercel.app, keep the
recorded fallback at https://runline-capstone.vercel.app/assets/saved-demo.html
in another tab, and leave downloads enabled. The unified site serves rebuilt
Charlottesville locally and forwards DC planning to the existing Oracle API.

## 1. Frame the progress — 30 seconds

**On screen:** the route form with the prefilled Rotunda start.

“RunLine helps runners find loops near a requested distance while favoring
fewer interruptions. Since our last presentation, we’ve improved loop quality,
built precomputed route serving, expanded to DC, and developed the processing
and deployment infrastructure behind those capabilities.”

“The controls will look familiar. Much of the new work is what happens behind
them: searching faster, serving saved candidates, and handling larger regions.”

## 2. Generate and compare — 60 seconds

**Click:** 5 miles → Mixed → Balanced → Here → Generate routes.

“We ask for a five-mile loop from the Rotunda. Each option starts and finishes
at the same mapped starting point. We show the actual distance, climbing,
signal count and trail percentage so the runner can compare alternatives.”

**Click:** second route card, then third. Point to the selected line.

“These are different route choices rather than one route repeated with tiny
changes. We rank candidates and remove alternatives with too much shared
street length.”

**Click:** GPX in the map summary.

“The selected route exports as GPX for compatible navigation apps and watches.”

## 3. Explain generation — 60 seconds

**On screen:** keep a selected five-mile route visible.

“We turn walkable streets and paths into a graph. Connections cost their
distance plus penalties for signals, busy roads and surface mismatch.”

“We choose two waypoints around the start and use weighted Dijkstra searches
to connect start to the first point, then the second, then home. We try many
directions and sizes. We remove dead-end detours, try alternate returns when
a route doubles back, and measure distance, crossings, terrain and shape.”

“Dijkstra finds the best connections under our costs. Our sampling, cleanup
and ranking logic turns those connections into useful loop alternatives.
We don’t claim to search every possible loop or guarantee a signal-free run.”

## 4. Explain precomputation and preferences — 60 seconds

**Click:** Trail → Generate routes. Point to trail percentages. Then restore
Mixed, click 8 miles → Generate routes.

“To avoid repeating the expensive search for every request, we divide the
region into roughly 200-meter starting cells. We generate candidates ahead
of time for one through twelve miles and three surface preferences.”

“At request time, we retrieve nearby pools, connect suitable loops to the
runner’s start, remeasure the complete route and rank it for the current
preferences. If too few fit, we search live.”

“Our rebuilt Charlottesville dataset contains 11,417 pools and 605,206 loops.
In a 600-request sample across three surfaces, 91.3 percent were answered
from saved pools. That’s a sample result, not a guarantee for every request.”

## 5. Show DC — 60 seconds

**Click:** Start → replace with “Washington Monument, Washington, DC” →
5 miles → Mixed → Balanced → Here → Generate routes.
Allow extra time for a first tile load; don't repeatedly submit.

“The same app now serves the Washington, DC pilot. Large metro areas are
split into overlapping tiles, and each starting cell identifies the graph
used to build its routes. The server loads matching data and caches a bounded
number of tile graphs.”

**Click:** another returned route card.

“This demonstrates the serving infrastructure beyond Charlottesville.
The wider top-50 rollout is still incomplete; the team has started the New
York and Los Angeles batch.”

## Optional: nearby starts — 30 seconds plus processing

Return to the default Charlottesville start by reloading. Choose 3 miles,
Mixed, Balanced, Better starts → 1 mi → Generate routes. Allow roughly ten
seconds. Point to the nearby-start labels and number checked.

“If the runner is willing to drive a short distance, we can compare nearby
public starting locations. This uses live planning and takes longer than a
normal saved-pool lookup.”

## Close — 20 seconds

“The major progress is a working route-pool pipeline and serving architecture,
expanded live coverage, corrected map data and stronger validation. Our suite
passes 196 tests. The rebuilt Charlottesville data has complete baked
elevation, and API rehearsals check that routes close, fit the requested
distance band and match their exported GPX tracks.”

If the network fails, switch to the recorded fallback and say: “This is a
saved response from the real planner. It lets us compare and export the
captured routes, but it is not generating a new route.”
