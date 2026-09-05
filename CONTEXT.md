# AI Dungeon Crawl

A harness for agents playing Dungeon Crawl Stone Soup.

## Language

**Observation**:
A complete view of the game at an input boundary or after exit, including menus and prompts.

**Action**:
One proposed game input, expressed as a single key. An action is not necessarily a game turn.

**Step**:
One completed transition consisting of the before observation, action, and after observation.

**Model turn**:
One model submission of a script for execution. It can produce zero or many game actions and is distinct from an in-game turn.
_Avoid_: Action (when referring to a whole script), game turn

**Turn record**:
A model turn together with its execution feedback and completed game steps. Grouping steps within a turn record identifies which model turn produced those actions.
