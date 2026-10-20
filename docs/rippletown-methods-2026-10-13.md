# Rippletown — Methods (revised draft)

Oct 13, 2026 · @Whansun

## Framework overview

Rippletown is a multi-agent simulation framework in which each individual is represented by a large language model (LLM) agent, while an engine manages the world those agents inhabit. The engine maintains space, simulated time, movement, bodily state and social interaction records. The LLM generates what an individual intends, says and attempts to do; the engine resolves the resulting requests under explicit world rules. This division separates generated behaviour from the mechanisms that update and record the simulated world.

An **engine contract** communicates those rules to the agents. World instructions are generated from the scenario configuration, while the output instructions and situation information supplied at each turn reflect the agent's available actions and current surroundings. Together they specify what an answer must look like, what commands mean, which people and places can be addressed, the current time and the agent's bodily condition. The engine-generated instructions are kept separate from the scenario's persona and background descriptions.

Each turn returns one structured answer: what the agent says and to whom, its expressed emotion and behaviour, and requests such as moving, taking others along, changing appearance or entering a state like sleep. The engine resolves structured requests under spatial and state constraints. Subsequent context reflects the resulting world state rather than treating every narrated intention as an accomplished action.

A scenario supplies the people, their personas and relationships, the map, a shared background and scheduled events. The same framework can therefore represent settings such as a household, a workplace or a school through different scenario definitions. It runs behind a web interface or headless from the command line and records dialogue, movement, state changes and time progression for inspection and analysis.

## Time and daily routines

Simulated activity proceeds in **waves**, each consisting of the agents taking a turn together. Messages generated in one wave help determine who participates in the next, alongside scheduled events, observations, state endings and scheduler re-invocation. Within a wave, each participant receives its incoming information and produces a turn. Turns are generated concurrently, after which the engine resolves their effects.

The simulated clock advances according to

\[
T_{w+1} = T_w + \Delta t_w,
\]

where \(T_w\) is elapsed simulated time at the start of wave \(w\), and \(\Delta t_w\) is the duration assigned to that wave. A separate LLM call estimates elapsed time directly from the scene, using the agents' utterances and actions together with the current time, their placement and the next scheduled event. It is asked to judge the duration of the scene, including implied activity between utterances, rather than the time needed to read the dialogue.

The engine constrains this estimate using pending events and state endings. Additional bounds limit large advances during active conversation and daytime activity, helping preserve opportunities for interaction. Time is therefore inferred from the generated scene but resolved within the temporal constraints of the world.

**Scheduled events** provide the external structure of daily routines. They can occur at a clock time, repeat on selected weekdays and apply within a range of simulated days. An event can remind an agent of an obligation, such as leaving for school or attending a class, and make it eligible for a turn. The scenario supplies the schedule; the agent generates its response in the context of its current situation and memory. Daily life thus combines prescribed obligations with generated interactions rather than requiring agents to reconstruct their routines without external cues.

During sustained inactivity, the engine advances time in increasing idle steps instead of repeatedly asking the time estimator. Pending events and state endings bound these advances. When the agents eligible for forced re-invocation are all asleep or otherwise occupied, the proposed advance targets the earliest release, unless a scheduled event or another state ending occurs first. The scheduler distinguishes inactivity from ongoing world activity while allowing time to pass through periods of limited interaction.

Each agent sees the current weekday and clock time at every turn. When sufficient simulated time has passed since its previous successful turn, or a new day has begun, a time stamp is also added to working memory. These labels distinguish a continuing exchange from a return to the conversation later in the day or on another day.

## Space and movement

The world is a graph of named **places** grouped into **zones**, with designated **exterior spaces** for locations outside the modelled environment. Places are the primary unit of co-presence and conversation. Zones support awareness of people elsewhere in the same setting, such as another room in a home, without opening a general conversation channel across rooms. Exterior spaces represent unmodelled destinations where ordinary spatial perception and communication are disabled.

Movement is resolved by the engine. When an agent names a destination, the engine selects a route that minimises zone-boundary crossings and then the number of steps. Movement proceeds one step at a time. A step within a zone has no separate travel duration; crossing into another zone places the agent in a travelling state for an engine-sampled interval. During travel, the agent is excluded from co-presence at either endpoint. Agents travelling together form a separate contact group.

A route passing through an intermediate zone forms a **journey**, and route information is made available to the agent. If another available person is present at a waypoint, the remaining journey pauses, allowing an encounter before onward movement. Reissuing the original destination resumes the journey, while selecting a different destination changes the route. The agent's prompt retains the intended destination so that a stop on the way does not erase the purpose of the trip.

## Agents

Each agent has its own persona, starting place, outward appearance, relationship map and memory context. Agents may share an underlying language model while receiving separate prompts. They are not given direct access to one another's private memories. Their world knowledge is supplied through the scenario background, their own interactions and the engine's perception mechanisms.

### Memory

At each turn, context is assembled from the agent's persona and engine instructions, the applicable shared background, its long-term memory, its recent working memory and temporary descriptions of the current time, situation and bodily condition.

**Working memory** holds the messages the agent has received and produced, each carrying a simulated-time stamp. As the assembled context approaches the agent's context budget, working memory is **compressed**. An LLM call reads the exchanges, grouped by simulated day and part of the day, alongside existing memory, and produces three kinds of long-term memory:

- **Episodes:** events describing intentions, actions, others' reactions, changes and unresolved matters.
- **Facts:** information intended to remain valid over time, such as traits, preferences, habits and standing ties, with a confidence score. Day-specific details are requested as episodes instead.
- **Relationships:** the agent's remembered assessments of other people, with earlier assessments retained as history.

The compression prompt asks for absolute day labels in place of relative expressions such as “today”, helping preserve temporal context across days. Following successful compression, the raw exchanges are archived and working memory is cleared for subsequent interactions.

New facts are compared with existing knowledge rather than simply appended. The engine retrieves relevant existing facts and presents them, with identifiers, to the compression model. The model judges whether a proposed fact is new, equivalent to an existing fact or a change to it. The engine checks the references before applying the corresponding update. Changed and merged facts retain historical versions and links to their supporting exchanges, allowing the evolution of stored knowledge to be inspected.

Long-term memory is rendered anew at each turn. Recent episodes receive more detail, older episodes are presented as selected highlights, and facts are prioritised by confidence. Episode text preserves the described event's temporal context, while numerical recency for this presentation is anchored to when the episode was stored during compression. A periodic LLM-based **consolidation** pass reviews the facts and may lower confidence in weakly supported details. This pass retains the records: facts omitted from the displayed memory can still be retrieved as candidates in later compression calls. Repeated mention can add supporting exchanges without automatically increasing confidence.

### Perception

Perception depends on spatial position and the observer's acquaintance with other agents. Primary message routing uses positions before the wave's movement is applied.

**Hearing.** An utterance normally reaches its addressees when they occupy the same place. A brief grace rule allows a directly addressed person who has just left to receive a parting response. Eligible direct messages can also be delivered after movement brings speaker and recipient together. These rules preserve conversational continuity around movement without providing a general channel between rooms. In spatial perception, others in the same place can overhear an exchange, labelled with who spoke to whom. Self-directed speech remains private, although its accompanying action description can be delivered as an observed scene.

**Sight.** Agents receive descriptions of arrivals, departures and the current appearance of those present, together with their own appearance. Last-seen information supports notices when a person looks different from a previous encounter. Within a zone, agents can be told who is elsewhere without those people being represented as visibly present. The contract instructs agents to distinguish co-present people from people known to be in other locations.

**Recognition.** An observer sees familiar people by name and unfamiliar people through appearance descriptions and observer-specific labels such as `stranger_1`. The engine's acquaintance rules determine when an unfamiliar person becomes known, as described below.

### Actions

Besides speaking, an agent can request four kinds of world update.

- **Movement.** A place starts a route or journey. Naming a person starts a pursuit whose route is updated toward that person's location. Naming the current place cancels remaining movement.
- **Accompaniment.** An agent can request that eligible co-located people travel with it, including someone who is asleep. Companions follow the leader's resolved movement and arrive together.
- **Appearance.** An agent can replace its outward description, for example when changing clothes, making the updated appearance available to observers.
- **States.** An agent can declare entry into a state such as sleep or a private activity.

### States

For a self-declared state, the agent chooses the kind of activity and the engine samples its duration from the corresponding range. The engine separately imposes a travelling state for movement across zones.

State-locked agents are excluded from ordinary scheduler re-invocation, and non-direct messages can be held until they are available. Natural state endings generate a notice and release held information in a bounded summary. Sleep or a private activity can still be interrupted by a direct address from someone nearby or by a scheduled notification; the agent can then decide whether to retain or leave the state. Travelling agents remain unavailable for ordinary interaction until arrival.

## Agent roles

Rippletown distinguishes **protagonists**, the individuals under observation, from **role-bound agents**, who provide services within the scenario. Protagonists choose their responses, movements and activities, while the scheduler returns available protagonists to the conversation when they have gone without a turn. Role-bound agents, such as a teacher at a school or a clerk at a shop, remain at a designated post and participate in response to incoming interactions or scheduled events rather than routine forced re-invocation.

| Property | Protagonists | Role-bound agents |
| --- | --- | --- |
| Participation | Incoming interactions, scheduled events, state releases and scheduler re-invocation | Incoming interactions and event-driven participation |
| Movement | Movement and accompaniment requests | Fixed post; their own movement requests are ignored |
| Shared background | Receive the scenario's shared background | Use their own persona without that shared background |
| Perception | Subject to spatial and acquaintance rules | Subject to the same rules and remain visible to other agents |

This distinction allows a scenario to include the institutions that organise daily life while keeping autonomous activity focused on the individuals under study.

## Social structure and perspective-dependent relationships

Social ties are specified from each agent's perspective. A relationship map associates another agent's identifier with a term such as “wife”, “husband” or “daughter”. Maps need not be symmetric: if A knows B but B does not know A, B initially perceives A as a stranger. The maps provide initial kinship, forms of address and acquaintance, while the engine removes references to nonexistent agents and self-references during initialisation.

Prompts connect known people to their identifiers and relationship terms so that a persona statement such as “you have a daughter” refers to an agent who can be addressed. Unknown people appear through appearance descriptions and stranger labels assigned separately for each observer. The same person can therefore be a named relative to one agent and a stranger to another. When a valid direct address to a stranger is resolved, the engine establishes mutual acquaintance. Overheard exchanges are labelled according to the listener's own knowledge.

Remembered interpersonal assessments can also change through compression. The relationship component of long-term memory records an agent's developing view of others, with earlier views retained for comparison. These subjective assessments are distinct from the initial relationship terms and from the engine's record of who has become acquainted with whom.

## Wave processing and scheduling

The engine uses a fixed processing order to establish what each agent can perceive and when its actions affect the world. Scheduled events are applied first, and agents whose states have ended are released. The participating agents then generate their turns concurrently. Primary speech and observation routing uses pre-movement positions, so a speaker's departure does not remove the audience for words spoken before leaving. Appearance, state and movement updates are resolved, followed by eligible post-movement message delivery and assembly of the next wave.

After the wave's duration is determined and the clock advances, contact-dependent processes are evaluated using the wave-start contact groups and that wave's elapsed time. For these calculations, the starting contact configuration is treated as constant over the interval. This defines the temporal approximation used to connect generated activity to contact-based outcomes.

Scheduling safeguards keep the simulation progressing through both interaction and inactivity. Available protagonists that have gone without a turn are re-invoked. When message routing yields no next participant, the engine considers world activity and agent availability before re-invoking agents or advancing through an idle interval. Persistent inability to generate successful turns triggers a no-progress stop rather than allowing a failed run to continue indefinitely.

Conversation ending is not itself a stopping condition. A run proceeds toward its requested duration, subject to its wave limit, continued presence of active agents and external or failure-related stopping conditions.

## Recording and reanalysis

Run configurations, dialogue, world updates and agent snapshots preserve the state needed for inspection and continuation. Snapshots include memory and runtime information such as location, route, activity state, held messages, acquaintances and last-seen appearances. Saved runs can be resumed or continued under a changed configuration; this supports continuation from recorded state rather than guaranteeing the same future trajectory as an uninterrupted execution.

Wave-start snapshots also record the active agents' locations and contact-relevant states alongside each wave's duration. These records allow contact-dependent outcomes to be recalculated under alternative transmission assumptions without generating the behaviour again. Such reanalysis holds the recorded behavioural trajectories fixed: agents do not change their actions in response to a different infection outcome produced during recalculation. It therefore describes alternative outcomes conditional on the original trajectories, rather than a new simulation of the coupled behavioural and infection processes.
