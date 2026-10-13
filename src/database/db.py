import os
from functools import lru_cache
from sqlalchemy import create_engine

@lru_cache(maxsize=1)
def get_engine():
    return create_engine(os.environ["DATABASE_URL"])