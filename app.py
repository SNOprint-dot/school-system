import os
import psycopg2
from flask import Flask, jsonify, request
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from auth_models import User
from db_config import get_db_connection

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default-key')

login_manager = LoginManager()
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

@app.route('/')
def home():
    return jsonify({"message": "Ghana School Management System API is Live!"})

# --- SECURITY & AUTHENTICATION ROUTES ---

@app.route('/api/register_admin', methods=['POST'])
def register_admin():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    # Scramble the password securely
    hashed_password = generate_password_hash(password)
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Save to the database with the 'admin' role
        cur.execute(
            "INSERT INTO system_users (email, password_hash, role) VALUES (%s, %s, %s) RETURNING user_id",
            (email, hashed_password, 'admin')
        )
        new_user_id = cur.fetchone()['user_id']
        conn.commit()
        return jsonify({"message": f"Admin registered successfully! User ID: {new_user_id}"}), 201
    except psycopg2.IntegrityError:
        conn.rollback()
        return jsonify({"error": "An account with this email already exists."}), 409
    finally:
        cur.close()
        conn.close()

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id, email, password_hash, role FROM system_users WHERE email = %s", (email,))
    user_data = cur.fetchone()
    cur.close()
    conn.close()

    # Verify user exists and the password matches the database hash
    if user_data and check_password_hash(user_data['password_hash'], password):
        user = User(user_data['user_id'], user_data['email'], user_data['role'])
        login_user(user) # This creates the secure session cookie
        return jsonify({"message": "Logged in successfully!", "role": user.role})
    
    return jsonify({"error": "Invalid email or password"}), 401

@app.route('/api/dashboard', methods=['GET'])
@login_required
def dashboard():
    # This route is locked! You can only see it if logged in.
    return jsonify({
        "message": f"Welcome to the secure control panel, {current_user.email}!",
        "role": current_user.role
    })

# --- TEMPORARY BROWSER TESTING UI ---

@app.route('/test_ui')
def test_ui():
    return """
    <html>
        <body style="font-family: Arial; padding: 20px; max-width: 600px;">
            <h2>SMS Security Testing Interface</h2>
            <p>Use this panel to test the API routes we just built.</p>
            
            <div style="background: #f4f4f4; padding: 15px; margin-bottom: 10px;">
                <h3>1. Register Admin</h3>
                <input type="email" id="regEmail" placeholder="admin@school.com" style="padding: 5px;">
                <input type="password" id="regPass" placeholder="Password" style="padding: 5px;">
                <button onclick="sendReq('/api/register_admin', 'regEmail', 'regPass')" style="padding: 5px;">Register</button>
            </div>
            
            <div style="background: #e9ecef; padding: 15px; margin-bottom: 10px;">
                <h3>2. Login</h3>
                <input type="email" id="logEmail" placeholder="admin@school.com" style="padding: 5px;">
                <input type="password" id="logPass" placeholder="Password" style="padding: 5px;">
                <button onclick="sendReq('/api/login', 'logEmail', 'logPass')" style="padding: 5px;">Login</button>
            </div>
            
            <div style="background: #d4edda; padding: 15px; margin-bottom: 10px;">
                <h3>3. Access Secure Dashboard</h3>
                <button onclick="testDashboard()" style="padding: 5px;">Check if I am logged in</button>
            </div>

            <pre id="output" style="background: #333; color: #0f0; padding: 15px; margin-top: 20px; white-space: pre-wrap;"></pre>

            <script>
                async function sendReq(endpoint, emailId, passId) {
                    const email = document.getElementById(emailId).value;
                    const password = document.getElementById(passId).value;
                    const res = await fetch(endpoint, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({email, password})
                    });
                    document.getElementById('output').innerText = await res.text();
                }
                async function testDashboard() {
                    const res = await fetch('/api/dashboard');
                    document.getElementById('output').innerText = await res.text();
                }
            </script>
        </body>
    </html>
    """

if __name__ == '__main__':
    app.run(debug=True)
