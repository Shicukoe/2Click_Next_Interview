import os

import psycopg
from psycopg.rows import dict_row


def connect():
    # Timestamps come back in Rome time, the time zone the sales team works in.
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, options="-c timezone=Europe/Rome")
