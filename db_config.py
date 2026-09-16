import os
import psycopg2
from psycopg2.extras import RealDictCursor

def get_db_connection():
    # This securely grabs the password from Render
    db_url = os.environ.get("DATABASE_URL") 
    return psycopg2.connect(db_url, cursor_factory=RealDictCursor)
