# Rippletown — Methods (revised draft)

Oct 1, 2026 · @Whansun

## Framework overview

Rippletown is a multi-agent simulation framework that separates LLM-based behavior generation from engine-managed world dynamics. Each individual is represented by an agent with its own persona and memory context. The engine maintains locations, simulated time, movement, configured physiological processes, and social interaction records. Agents generate utterances and behavioral intentions; the engine resolves structured requests through explicit rules. Some engine processes, including duration sampling and the optional infection model, are stochastic. This separation makes the rules governing world updates explicit without implying that complete simulation trajectories are deterministic.

An **engine contract layer** communicates these rules to the agents. Static world instructions are assembled from the run configuration when agents are initialized. The output contract is generated when constructing each turn's system message, and current time, situation, and bodily-condition information are supplied as applicable. Engine-generated instructions are kept separate from scenario-authored personas and background. A verification function checks for required instruction strings associated with selected enabled features and returns warnings when they are missing. This is a prompt consistency check, not a proof that every feature is covered or that the LLM will comply. Because generated contracts can change with engine updates, the engine version is part of the experimental specification.

Each agent turn is requested as a single JSON object containing an utterance (`content`), addressees (`target`), configured metadata such as emotion and action type, and a free-text behavioral description (`action_note`). Optional requests include movement (`move_to`), accompaniment (`bring_along`), appearance changes (`update_appearance`), and entry into a configured state (`enter_state`). The engine validates structured requests against its implemented spatial and state constraints and provides subsequent context reflecting the resolved world state. Free-text behavior and appearance descriptions are not subject to general physical-consistency validation.

The framework is implemented in Python. The simulation engine is independent of the interface and can run behind a FastAPI web application with server-sent events or headless from the command line. Agent turns within a wave are issued concurrently to configured model endpoints, including locally hosted models served with vLLM. Run configurations, turn records, supported events, and agent snapshots are persisted in SQLite when database persistence is enabled. Runs can be inspected, resumed, and exported as Markdown transcripts or CSV location histories.

The descriptions below distinguish supported mechanisms and implementation defaults from the settings actually used in an experiment. Experiment-specific settings remain to be documented in the final subsection.

## Time and wave-based evolution

A **wave** is a batch of agent turns generated concurrently and then processed by the engine. Conversation-driven scheduling follows a breadth-first pattern: recipients of messages generated in one wave can participate in the next. This scheduling is augmented by scene observations, scheduled events, state releases, director interventions, and re-invocation rules. Wave membership is therefore not determined solely by explicit conversational addressees.

Let \(T_w\) be elapsed simulated time in minutes at the start of wave \(w\). Time evolves as

\[
T_{w+1}=T_w+\Delta t_w.
\]

The implementation supports three time-advance paths:

\[
\Delta t_w=
\begin{cases}
\delta_{\mathrm{fixed}}, & \text{fixed mode},\\
\min\!\left(\widehat{\Delta t}_w,C_w^{\mathrm{variable}}\right),
  & \text{ordinary variable-mode advance},\\
\min\!\left(J_w,C_w^{\mathrm{idle}}\right),
  & \text{variable-mode idle advance}.
\end{cases}
\]

Here \(\delta_{\mathrm{fixed}}\) is the configured constant interval, \(\widehat{\Delta t}_w\) is the raw variable-mode estimate, and \(J_w\) is an idle interval. Each cap is the minimum of the limits applicable to that path; an absent limit is treated as infinite. In variable mode, a wave with neither a successful turn nor an idle advance does not accumulate time.

### Variable time estimation

For an ordinary variable-mode advance, a separate LLM call receives the wave's utterances and action descriptions, current time, a placement summary, and the next scheduled event when available. In category mode, it selects a scene category and the engine samples an integer duration uniformly from the category's inclusive range. In direct-estimation mode, it returns an integer number of minutes, bounded by the configured overall category range and the estimator's event-related upper bound. A failed direct estimate falls back to duration sampling from the normal-scene category, or the configured fallback category if that category is absent.

Default categories are brief or transitional scenes (5–10 min), ordinary conversation or activity (15–30 min), agents alone or off-screen (60–120 min), and night-time sleep (240–420 min). The estimator is instructed to assess the duration of the whole scene, including implied off-screen activity, rather than the reading time of dialogue. These estimates define simulated narrative time; they are not measurements of elapsed human activity.

The engine then applies the smallest applicable limit:

1. **Scheduled event:** the interval remaining until the next pending clock-time event.
2. **State release:** the interval remaining until the earliest active state expires.
3. **Target duration:** the interval remaining until the requested run duration is reached.
4. **Scene cap:** 45 min by default when at least one addressed utterance reaches another agent and at least two successful speakers with nonempty utterances have the same non-exterior location after movement. This is a scene-protection heuristic; it does not require those two speakers to have addressed each other.
5. **Daytime cap:** 180 min by default between 06:00 and 22:00 when at least one successful speaker with a nonempty utterance has a non-exterior location after movement. This condition examines the wave's speakers, not all occupants of a designated home.

Scenario authors can schedule events at clock times and repeat them on selected weekdays, for example at 16:30 on Monday, Wednesday, and Friday. In fixed mode, these events are evaluated at wave boundaries; the constant increment is not shortened by the variable-mode caps, so an event can be processed after its nominal time.

### Idle advances and stopping

Collective silence is detected when message and scene routing leave no eligible participant in the next-wave queue. From the second consecutive occurrence, variable mode uses an idle schedule of 60, 120, then 180 min, saturating at the last configured value. When all agents in the forced-call pool are state-locked, the raw idle interval instead targets the earliest release in that pool. The pool normally contains protagonists; an all-role-bound scenario falls back to its active agents.

Idle advances are separately limited by the next pending scheduled event and the earliest active state release. They do not pass through the ordinary variable-mode scene, daytime, or target-duration cap. Consequently, an earlier event may interrupt a proposed jump to a state release, and an idle advance may overshoot the requested run duration. Fixed increments can also overshoot the target. The run checks the duration stopping condition after completing a wave. Other stopping conditions include the maximum wave count, no remaining active agents, external interruption, and repeated generation failure.

Each agent receives the current weekday and clock time in its turn context. With memory time anchors enabled, an absolute day-and-time label is also added to working memory when the gap since the agent's previous successful turn reaches the configured threshold (45 simulated minutes by default), or crosses a day boundary. The comparison time is updated after each successful turn, including turns that need no label. This is intended to distinguish events across days rather than leave all remembered conversation temporally adjacent.

## Space and movement

The world is a graph of named **place nodes**, optionally grouped into **zones**, with designated **exterior nodes**. These are spatial categories rather than three nested graph levels. A place, such as a kitchen or classroom, is the primary unit of co-presence. A zone, such as a home or school, supports awareness of people in its other places without generally allowing conversation across rooms. Exterior nodes represent unmodeled locations; ordinary spatial communication is disabled for agents there.

Edges connect places and may also refer to a zone. Zone references are expanded so that entry leads to the zone's designated entrance and exit can be available from its constituent places. Spatial rules then operate on the expanded adjacency graph. For a requested destination, the engine selects a path minimizing zone-boundary crossings first and hop count second. Movement progresses by at most one hop per movement phase. Within-zone hops have no additional travel duration. With zone travel enabled, crossing a zone boundary imposes a *traveling* state with an integer duration sampled uniformly from 10–20 simulated minutes by default. The traveler is excluded from ordinary co-presence and communication until arrival.

A route with an intermediate zone-boundary arrival can form a **journey** with **waypoints**. For example, in a scenario whose graph connects school and a shop through a home's entrance, the route passes through that entrance; this follows from the scenario topology rather than a universal routing rule. The agent receives route context. At a waypoint with another available person, the engine pauses the remaining path and retains the journey destination. Reissuing that destination resumes the trip; choosing a different destination changes the movement intention. Without an available person at the waypoint, the route continues. Situation prompts retain the paused destination and resumption instruction.

## Agents

Each agent has a separate persona, working memory, long-term memory records, initial location, appearance, and perspective-specific relationship specification. This does not require a separate model process or checkpoint for each agent. Agents can use a shared model endpoint while receiving different contexts. Another agent's private memory is not directly included in an agent's prompt; world information is supplied through configured background and the engine's perception and message mechanisms.

### Memory

Turn context is assembled from the system message, including persona and applicable engine instructions; shared background where applicable; a rendered long-term memory block; recent working memory; and ephemeral time, situation, and bodily-condition messages.

Working memory stores messages received and produced by the agent, with internal simulated-minute timestamps. When the estimated prompt reaches 70% of the agent's token budget and working memory contains at least four entries, the engine attempts compression. The default budget is 8,192 tokens. Prompt length is estimated from UTF-8 byte length with message overhead rather than measured with the served model's tokenizer. Additional trimming can remove working-memory entries if needed to fit the estimated budget.

Compression uses an LLM call supplied with existing structured memory and the recent messages, grouped under absolute simulation day, weekday, and half-day headers. It returns four components:

- **Episodes:** experienced events with importance scores.
- **Facts:** persistent statements with confidence scores; day-specific information is requested as episodes instead.
- **Relationships:** the agent's remembered assessments of other people.
- **Self-state:** current goals and condition.

After successful compression, raw messages are archived, structured memory is updated, and working memory is cleared. If compression fails, existing working memory is retained subject to subsequent token-budget trimming. The compression prompt requests absolute day labels instead of ambiguous references such as “today”; this is an instruction to the model, not a guarantee of temporal accuracy.

The current implementation timestamps newly stored episodes numerically with their **compression time**, while event dates can remain in their textual descriptions. Memory display uses this numerical timestamp. For an episode stored at \(t_e\), its integer age is

\[
a_e=\max\!\left(0,\left\lfloor\frac{T-t_e}{1440}\right\rfloor\right).
\]

Episodes with \(a_e\leq1\) enter the recent bucket, with up to ten displayed. Thus, the recent bucket can include entries stored less than 48 simulated hours earlier; it does not implement a strict 24-hour window from the original event. For older entries, only the three highest-importance episodes are displayed, with a count of omitted entries and a less-specific recollection label. Facts are ranked by confidence and capped at fifteen displayed entries.

After every third successful compression, an LLM-based consolidation pass reviews the agent's facts and proposes confidence adjustments. It is instructed to strengthen repeatedly supported information and weaken unsupported incidental details. Records are retained rather than deleted by this pass, allowing low-confidence facts to become visible again if their ranking changes. These mechanisms impose a bounded memory presentation and are intended to encourage persistence of repeated information; they do not establish a validated model of human forgetting.

### Perception

**Hearing.** Primary utterance routing uses positions before the wave's movement phase. An addressed utterance normally reaches agents in the same place. For explicit individual targets, a one-wave grace rule also permits delivery when speaker and recipient were co-located at the start of the preceding wave, subject to exterior and traveling exclusions. Broadcasts do not receive this grace. A supplementary post-movement pass can deliver eligible directly addressed utterances when movement brings speaker and recipient together under the routing rules. These exceptions make co-presence the primary communication rule rather than an absolute start-of-wave-only condition.

In spatial perception mode, eligible people in the same place may overhear speech, tagged with speaker and addressee. Private utterances are not broadcast as speech, although accompanying action descriptions can be delivered as observed scenes. Repeated self-state-maintenance actions are suppressed in the relevant routing path to avoid generating turns from recurring sleep or inactivity descriptions.

**Sight.** Situation context describes co-present people, their current appearances, and the observer's own appearance. Arrival, departure, and appearance-change observations are generated by the engine. Last-seen appearance records support notices when a person looks different from the observer's previous encounter. Within a zone, agents can also receive awareness of people in other places; this does not itself open a cross-room conversation channel.

**Recognition.** Known individuals can be presented by their names and relationship terms. Unknown individuals are represented by appearance and observer-specific identifiers such as `stranger_1`. Recognition follows the engine's acquaintance rules described below rather than semantic verification that a name has actually been exchanged in dialogue.

### Actions and states

Agents can request movement to a place, pursuit of a person, or remaining at the current location. A place request establishes a path; a person request allows the engine to update the route toward that person's location and resolve converging pursuits. Naming the current location cancels remaining movement. With `bring_along`, eligible co-located companions follow the leader's resolved path. Appearance requests replace the agent's outward description and can generate observations for others.

Agents may also select a configured state category through `enter_state`. The engine samples an integer duration from that category's range rather than accepting an agent-selected duration. Default ranges are 300–540 min for sleep and 10–30 min for private activity. Traveling is imposed by movement rather than selected as a self-declared activity.

State-locked agents are excluded from ordinary forced re-invocation. Traveling blocks ordinary interaction; self-declared sleep or private activity can be interrupted by an eligible direct address, scheduled notification, or director intervention. When given such a turn, an agent can retain or leave its self-declared state. Non-direct messages may be held while the agent remains locked and are delivered through a bounded summary mechanism when the agent receives an eligible turn or its state expires. Natural expiration generates a release notice and can return the agent to the current wave. Held-message delivery is selective and bounded, not an unabridged replay of everything that occurred during the state.

## Agent roles

Rippletown distinguishes **protagonists**, the individuals under observation, from **role-bound agents**, such as a doctor at a clinic or a teacher at a school. Role-bound agents support interactions at fixed posts while avoiding routine forced turns that would make them additional protagonists.

| Property | Protagonists | Role-bound agents |
| --- | --- | --- |
| Turn scheduling | Message delivery, events, state releases, and scheduler re-invocation | Eligible message or event-driven participation; ordinarily excluded from forced re-invocation |
| Movement | Structured movement and accompaniment requests | Fixed post; own movement requests are ignored |
| Director monitoring | Included in silence, isolation, and repetition monitoring | Excluded from protagonist monitoring and intervention targeting |
| Shared scenario background | Included as configured | Omitted; agent-specific persona remains |
| Visibility and routing | Governed by spatial and acquaintance rules | Governed by the same rules; visible in placement summaries |

The forced-call pool falls back to active agents when a scenario contains no protagonists. Role-bound agents remain in the director's placement summary so that institutional occupants remain represented in its world context.

## Perspective-dependent social structure

Initial relationships are specified from each agent's point of view as a map from another agent's identifier to a relationship term. Maps need not be symmetric: A can know B while B initially perceives A as a stranger. Initialization removes self-references and references to nonexistent agents and warns about one-directional ties.

Prompts bind known people to identifiers and relationship terms, connecting persona descriptions to addressable agents. Unknown people are shown through appearance descriptions and observer-specific stranger identifiers. When an eligible direct address to a stranger is resolved, the engine establishes mutual acquaintance. This is an operational recognition rule; it does not require a verified exchange of names. Overheard utterances are labeled using the listener's knowledge, so the same speaker can be named for one listener and anonymous for another. When no relationships are specified anywhere in the scenario, the default is mutual knowledge and the stranger mechanism is disabled.

The initial relationship map, dynamically acquired acquaintance, and remembered interpersonal assessments are distinct. Compression can update subjective assessments stored in long-term memory, with previous assessments retained as history. Such updates need not change the initial kinship labels or follow an explicit exchange in dialogue.

## Director agent

An optional **director** is a separate LLM call invoked at the start of configured waves, before participant turns. Its context includes a recent activity digest, placements, monitored protagonists' silence and isolation indicators, a repetition indicator, an author-specified narrative note, and a running memo capped at twelve lines. It is not directly supplied with agents' private memory stores or relationship maps. Its access to global logged activity nevertheless exceeds the local perception of an individual agent.

The digest defaults to six waves, configurable within two to twenty, with at most 120 lines and length-limited utterance excerpts. Repetition is calculated over up to four recent eligible utterances using the maximum pairwise character-sequence similarity from Python's `SequenceMatcher`. A score of at least 0.65 flags repetition. Filler-only speech can be replaced by the accompanying action description for this calculation. This measure detects surface similarity; the director is asked to assess topical repetition from the digest.

The director returns zero or more short interventions addressed to one or several agents. Its prompt instructs it to provide perceptible environmental stimuli, avoid acting as a character, avoid asserting completed agent actions or unsupported objects and states, and use the current time. These are model instructions rather than comprehensive engine-enforced semantic restrictions. Interventions are delivered as context and do not directly execute movement or other structured actions, but they can affect subsequent agent choices. Director settings therefore form part of the experimental condition.

## Wave processing and scheduling

The main processing order is fixed, although the participating agents and generated outputs vary. Concurrently returned turn results are sorted by agent identifier before the principal routing and world-update passes. This stabilizes processing order against response-completion order without making model outputs or all execution effects deterministic.

**Algorithm 1. Processing a wave**

1. Evaluate scheduled events at the current simulated time and update active participants.
2. Expire due states, resolve applicable arrival and journey conditions, and enqueue release notices and held messages.
3. Apply empty-wave safeguards, optional director interventions, and state-related participation gates.
4. Construct each participant's context, perform memory compression or consolidation when due, and generate participant turns concurrently.
5. Process results in agent-key order; route utterances, overheard speech, and observed actions using pre-movement positions and applicable grace rules. Apply appearance and self-declared state updates.
6. Resolve movement, accompaniment, pursuit, and journey effects. Perform eligible supplementary post-movement message delivery.
7. Evaluate the optional contact-dependent infection process after movement. Assemble the next-wave queue, hold messages for unavailable agents, and apply silence and starvation rules.
8. Advance simulated time through the applicable fixed, ordinary variable, or idle path, then check the duration stopping condition.

Contact-dependent processes require their own model specification; their placement in this sequence does not by itself define how exposure depends on wave duration.

Three safeguards support continued activity:

- **Starvation re-invocation:** an available protagonist without a turn for the configured number of waves is added with empty incoming messages. The interval is measured in waves, not simulated minutes.
- **Collective silence and empty-wave recovery:** the first empty routed next-wave queue triggers available forced-call candidates; subsequent consecutive occurrences select starved candidates and, in variable mode, permit idle advances. At the start of a wave, an empty queue still remaining after event and state processing invokes the empty-wave fallback.
- **No-progress stop:** the run stops after \(\max(6,2s)\) consecutive waves with no successful turn, where \(s\) is the starvation threshold. This identifies persistent inability to generate turns; it does not uniquely diagnose a model-server failure.

LLM calls occur in participant generation, memory compression and consolidation, the optional director, and ordinary variable-mode time estimation. The remaining mechanisms combine explicit rule execution with configured random sampling. Conversation ending is not itself a stopping condition when active agents remain eligible for continued simulation.

## Persistence, failure handling, and reproducibility

Agent outputs are parsed as JSON. The language-retry mechanism detects configured CJK ideograph ranges and requests regeneration, with up to two retries by default; it is not a general detector of every unintended language or script. On a generation or parsing failure, the engine attempts to remove newly injected incoming messages from working memory. The current execution path does not guarantee automatic redelivery of those messages on the next turn.

Persistence includes working memory and runtime fields such as location, path, journey, state, held messages, acquaintance, and last-seen appearance, together with run configuration and supported event logs. These records support inspection and continuation from saved state. They do not guarantee a continuation identical to an uninterrupted run: some scheduler counters are initialized on a new run invocation, the snapshot mechanism does not establish complete random-state restoration, and future LLM responses can differ.

The CLI seed controls Python's engine-side pseudorandom sampling. It does not by itself seed model decoding, and concurrent runs using the same process share the global random generator. Engine-seed comparisons therefore require an execution setup that avoids such interleaving, alongside separately documented model sampling controls. A locally served model supports rerunning the software without a proprietary inference API, but this is distinct from reproducing identical output trajectories.

Regression tests assess implemented software behavior. Their count and passing status should be tied to a reported code revision and test command; they are not evidence that simulated behavior is empirically valid. Duration ranges, time caps, scheduling thresholds, memory-display limits, and director rules are implementation heuristics unless supported by a separate calibration or validation study.

## Experimental configuration to complete before submission

The original draft identifies Qwen3.6-35B-A3B as the experimental model, and the repository's default model identifier is `Qwen/Qwen3.6-35B-A3B`. A default identifier does not verify which checkpoint or settings generated the reported results. The following experiment-specific information remains to be completed from the actual run records; the framework defaults above must not be substituted for those records.

| Item | Information required for the reported experiments |
| --- | --- |
| Software | Engine commit or release, dependency versions, vLLM version, hardware and precision or quantization settings |
| Models | Served checkpoint and revision for agents and any auxiliary LLM calls; any per-agent overrides |
| Generation | Temperature, top-p/top-k where applicable, output token limits, reasoning settings, retry settings, and model-side seed policy |
| Scenarios | Agent personas and roles, initial locations and appearances, relationship maps, graph and zone definitions, shared background, and scheduled events |
| Time and scheduling | Fixed or variable mode, category or direct estimation, all ranges and caps, idle schedule, starvation threshold, start time, target duration, and maximum waves |
| Perception and memory | Perception mode, grace and time-anchor settings, token budgets, compression and consolidation settings, and persistence mode |
| Director and additional processes | Director enabled/disabled, invocation interval and prompts; infection-model settings and a precise Methods cross-reference if used |
| Replication | Number of independent runs, engine and model seed policy, concurrency, resumed runs, actual durations, and failure or exclusion rules |
| Validation | Outcome measures, empirical comparisons or calibration where available, sensitivity analyses, and regression-test command and results for the reported revision |

Archive the resolved configurations and prompt templates with the experiment artifacts. Any claim that these mechanisms improve realism, continuity, or memory fidelity should be supported by the corresponding evaluation rather than inferred from their implementation alone.
