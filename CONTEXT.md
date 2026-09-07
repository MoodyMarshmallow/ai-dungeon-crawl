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
One decision cycle beginning with game feedback and ending with an accepted script submission for execution. It can contain one or more model turns; script execution supplies feedback for the next agent turn rather than ending the episode.
_Avoid_: Model turn (when referring to the whole decision cycle), game turn

**Model turn**:
One logical invocation of the model and its generated response within an agent turn. Reasoning, intermediate text, and tool calls can be parts of that response; a tool result is feedback, not a model turn, and requesting the model's continuation begins another model turn.
_Avoid_: Agent turn, individual streamed event

**Agent Turn record**:
An agent turn's script submission together with its execution feedback and completed game steps. Grouping steps within a turn record identifies which agent turn produced those actions.

**Game Episode**:
One game session encompassing successive agent turns and their executions until the game ends or the harness stops the run.
