from langgraph.checkpoint.memory import MemorySaver
import logging
from typing import Any, Dict

class SafeMemorySaver(MemorySaver):
    """A MemorySaver that skips non-serialisable callables before storing state.

    This prevents MsgPack errors like `Type is not msgpack serializable: function` that occur if
    a callable accidentally ends up in the LangGraph state.  We walk the state dict recursively
    and drop any value that is `callable`.  Containers (dict / list / tuple) are cleaned in place,
    but we **never** mutate the original references – we build a shallow copy so that the runtime
    state is untouched.
    """

    def _clean_value(self, value: Any) -> Any:
        """Recursively remove callables from the supplied value."""
        if callable(value):
            logging.warning("SafeMemorySaver: stripped callable %s from state before checkpoint", value)
            return None  # Skip callables entirely
        if isinstance(value, dict):
            return {k: self._clean_value(v) for k, v in value.items() if not callable(v)}
        if isinstance(value, (list, tuple)):
            cleaned = [self._clean_value(v) for v in value if not callable(v)]
            return type(value)(cleaned)  # Preserve original container type
        # Primitive / unknown types – return as-is
        return value

    # ------------------------------------------------------------------
    # Public API overrides
    # ------------------------------------------------------------------

    def put(self, *args, **kwargs):  # Accept any arguments the parent expects
        """Clean the state before deferring to the parent implementation."""
        # Extract the state from args based on parent's expected signature
        # LangGraph's MemorySaver.put() typically expects: (config, checkpoint, metadata, new_versions)
        # or similar variations depending on the version
        
        try:
            # Try to identify which argument is the state dict
            state = None
            state_index = None
            
            for i, arg in enumerate(args):
                if isinstance(arg, dict) and any(key in arg for key in ['messages', 'state', 'data']):
                    state = arg
                    state_index = i
                    break
            
            if state is not None and state_index is not None:
                # Clean the state
                cleaned_state = {k: self._clean_value(v) for k, v in state.items() if not callable(v)}
                # Replace the state in args with the cleaned version
                args_list = list(args)
                args_list[state_index] = cleaned_state
                args = tuple(args_list)
            
            # Call parent with all arguments
            super().put(*args, **kwargs)
            
        except TypeError as e:
            logging.error(
                "SafeMemorySaver failed to serialise state even after cleaning: %s", e, exc_info=True
            )
            # Try to create minimal args for fallback
            try:
                # Create a minimal state
                minimal_state = {
                    "__error__": "serialization_error",
                    "__details__": str(e),
                }
                
                # Try to call parent with minimal state
                if len(args) >= 1:
                    args_list = list(args)
                    # Find and replace the state-like argument
                    for i, arg in enumerate(args_list):
                        if isinstance(arg, dict):
                            args_list[i] = minimal_state
                            break
                    super().put(*tuple(args_list), **kwargs)
                else:
                    # If no args, just log and continue
                    logging.error("SafeMemorySaver: No arguments to work with, skipping checkpoint")
                    
            except Exception:
                # If this also fails, log and swallow – better to lose the checkpoint than crash.
                logging.exception("SafeMemorySaver could not write even the minimal fallback state.") 