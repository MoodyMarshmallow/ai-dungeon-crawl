from ..contracts import GameAction, GameObservation


class MockGameSession:
    def __init__(self) -> None:
        self.position = 0
        self.closed = False

    def _observe(self) -> GameObservation:
        row = ["."] * 4
        row[self.position] = "@"
        return GameObservation(
            id=self.position,
            screen="######\n#" + "".join(row) + "#\n######",
            ended=self.position == 3,
        )

    async def start(self) -> GameObservation:
        return self._observe()

    async def step(self, action: GameAction) -> GameObservation:
        if self.closed or self.position == 3:
            raise RuntimeError("Session is not accepting input")
        if action.key != "l":
            raise ValueError("This mock accepts only 'l' (move right)")
        self.position += 1
        return self._observe()

    async def close(self) -> None:
        self.closed = True
