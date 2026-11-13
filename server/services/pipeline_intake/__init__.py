"""Zero-credential intake for failed data pipeline runs (dbt, Airflow, raw logs).

The evidence is whatever the caller sends: dbt artifacts, an Airflow
``on_failure_callback`` payload or log text. Nothing here talks to Airflow,
dbt Cloud or a warehouse.
"""
