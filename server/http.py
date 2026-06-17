"""HTTP server for remote control of Ash."""

from fastapi import FastAPI

app = FastAPI(title="Ash Remote Control")


@app.post("/turn")
async def run_turn(input: dict) -> dict:
    user_input = input.get("input", "")
    result = await app.state.ash_loop.run_turn(user_input)
    return {"result": result}


@app.on_event("startup")
async def startup() -> None:
    """Wire ash_loop into app state. Caller must set app.state.ash_loop before starting."""
    if not hasattr(app.state, "ash_loop") or app.state.ash_loop is None:
        raise RuntimeError(
            "app.state.ash_loop must be set to an AshLoop instance before starting the server. "
            "Example: app.state.ash_loop = ash_loop"
        )
