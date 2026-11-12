# Aurora take-home submission draft

Option C: prevent loss of correlated context during RCA investigations.

## Argument

I found and fixed three ways Aurora could silently lose new evidence during an
ongoing RCA: batch formatting omitted later alerts, non-atomic Redis consumption
could delete newly arrived updates, and request-only context injection did not
persist into subsequent model turns.

This matters when correlated alerts or deployment updates contain information
needed to distinguish competing explanations. Aurora could acknowledge receiving
updates while continuing its investigation without their full context.

I reproduced each defect before fixing it, added regression tests, and kept the
production changes within two files. Targeted checks passed 132 tests, with one
opt-in live-model test skipped. Separate live-model and deployed
Celery/database/API checks confirmed that multiple context updates were retained
without duplication. This establishes the delivery fixes locally; it does not
demonstrate improved diagnosis accuracy on production incidents.

## Additional local adaptation

Separately, I added DeepSeek support because I prefer lower-cost models for
experimentation. The aim is to give new users another budget-friendly option
when evaluating Aurora, lowering the cost barrier to trying their first
investigation. This adaptation is separate from the RCA correctness fixes
described above.

## What I would do next

My original goal was to let engineers guide an investigation while it is
running. I would build on the existing progress display and context mechanism
to add a small interface for submitting information, plus cooperative pause and
resume at safe execution boundaries.

Aurora would continue automatically when nobody is available. An engineer could
provide a request ID, narrow the affected environment, or correct an assumption
without restarting the investigation or losing collected evidence.

The expected benefit is less unnecessary querying and faster access to
actionable findings. I would evaluate this using the same incident scenarios
with and without intervention, comparing tool calls and completion time while
checking that evidence quality is preserved.
