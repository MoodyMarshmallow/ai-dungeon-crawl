# AI Dungeon Crawl

A harness for agents playing Dungeon Crawl Stone Soup.

## Language

**Game Observation**:
A complete view of the game at an input boundary or after exit, including menus and prompts.

**Game Action**:
One proposed game input, expressed as a single key. An action is not necessarily a game turn.

**Game Step**:
One completed transition consisting of the before observation, action, and after observation.

**Agent turn**:
One decision cycle by either the Action agent or Review agent, beginning with feedback and ending with an accepted script submission for execution. It can contain one or more model turns; feedback need not be game feedback.
_Avoid_: Model turn (when referring to the whole decision cycle), game turn

**Action agent**:
The agent that plays the game by choosing and submitting game actions during a game episode.

**Review agent**:
The agent that examines model and game-state trajectories after a game and devises prompt improvements, tools, or skills to improve subsequent action episodes within the same session. A new session starts from a fresh baseline.

**Review phase**:
The post-death analysis period in which the Review agent works, ending when the review turn limit is reached.

**Session**:
A bounded sequence of game episodes and their post-death review phases.

**Model turn**:
One logical invocation of the model and its generated response within an agent turn. Reasoning, intermediate text, and tool calls can be parts of that response; a tool result is feedback, not a model turn, and requesting the model's continuation begins another model turn.
_Avoid_: Agent turn, individual streamed event

**Agent Turn record**:
An agent turn's script submission together with its execution feedback and completed game steps. Grouping steps within a turn record identifies which agent turn produced those actions.

**Game Episode**:
One game only, encompassing successive Action agent turns and their executions until that game ends or the harness stops the run. A review phase is outside the game episode.
