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

# --- SYSTEM SETUP ---
@app.route('/api/setup_db')
def setup_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS students (
            student_id SERIAL PRIMARY KEY,
            first_name VARCHAR(100) NOT NULL,
            last_name VARCHAR(100) NOT NULL,
            guardian_name VARCHAR(100) NOT NULL,
            guardian_contact VARCHAR(20) NOT NULL,
            enrollment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"message": "Student table successfully verified/created in Neon!"})

# --- SECURITY & AUTHENTICATION ROUTES ---
@app.route('/api/register_admin', methods=['POST'])
def register_admin():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    hashed_password = generate_password_hash(password)
    conn = get_db_connection()
    cur = conn.cursor()
    try:
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

    if user_data and check_password_hash(user_data['password_hash'], password):
        user = User(user_data['user_id'], user_data['email'], user_data['role'])
        login_user(user)
        return jsonify({"message": "Logged in successfully!", "role": user.role})
    return jsonify({"error": "Invalid email or password"}), 401

@app.route('/api/dashboard', methods=['GET'])
@login_required
def dashboard():
    return jsonify({"message": f"Welcome to the secure control panel, {current_user.email}!", "role": current_user.role})

# --- STUDENT MANAGEMENT ROUTES (CRUD) ---

# 1. CREATE (Enroll)
@app.route('/api/enroll_student', methods=['POST'])
@login_required
def enroll_student():
    if current_user.role != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
        
    data = request.get_json()
    first, last = data.get('first_name'), data.get('last_name')
    guardian, contact = data.get('guardian_name'), data.get('guardian_contact')

    if not all([first, last, guardian, contact]):
        return jsonify({"error": "Missing student details!"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO students (first_name, last_name, guardian_name, guardian_contact) VALUES (%s, %s, %s, %s) RETURNING student_id",
        (first, last, guardian, contact)
    )
    new_id = cur.fetchone()['student_id']
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"message": f"Student {first} {last} successfully enrolled with ID: {new_id}"}), 201

# 2. READ (View Roster)
@app.route('/api/students', methods=['GET'])
@login_required
def get_students():
    if current_user.role != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
        
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT student_id, first_name, last_name, guardian_name, guardian_contact, enrollment_date::text FROM students ORDER BY student_id DESC")
    students = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify({"status": "success", "total_students": len(students), "data": students}), 200

# 3. UPDATE (Edit Guardian Contact)
@app.route('/api/students/<int:student_id>', methods=['PUT'])
@login_required
def update_student(student_id):
    if current_user.role != 'admin':
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json()
    new_contact = data.get('guardian_contact')

    if not new_contact:
        return jsonify({"error": "Please provide a new contact number."}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE students SET guardian_contact = %s WHERE student_id = %s RETURNING student_id",
        (new_contact, student_id)
    )
    updated = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    if updated:
        return jsonify({"message": f"Student ID {student_id} contact updated successfully!"}), 200
    return jsonify({"error": "Student not found!"}), 404

# 4. DELETE (Remove Student)
@app.route('/api/students/<int:student_id>', methods=['DELETE'])
@login_required
def delete_student(student_id):
    if current_user.role != 'admin':
        return jsonify({"error": "Unauthorized"}), 403

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM students WHERE student_id = %s RETURNING student_id", (student_id,))
    deleted = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    if deleted:
        return jsonify({"message": f"Student ID {student_id} has been permanently deleted."}), 200
    return jsonify({"error": "Student not found!"}), 404


# --- TEMPORARY BROWSER TESTING UI ---
@app.route('/test_ui')
def test_ui():
    return """
    <html>
        <body style="font-family: Arial; padding: 20px; max-width: 600px;">
            <h2>SMS Testing Interface</h2>
            
            <div style="background: #f4f4f4; padding: 15px; margin-bottom: 10px;">
                <h3>1. Admin Auth</h3>
                <input type="email" id="email" placeholder="admin@school.com" style="padding: 5px;">
                <input type="password" id="pass" placeholder="Password" style="padding: 5px;"><br><br>
                <button onclick="sendAuth('/api/login')" style="padding: 5px;">Login as Admin</button>
            </div>
            
            <div style="background: #e2e3e5; padding: 15px; margin-bottom: 10px;">
                <h3>2. Enroll Student</h3>
                <input type="text" id="fName" placeholder="First Name" style="padding: 5px;">
                <input type="text" id="lName" placeholder="Last Name" style="padding: 5px;"><br><br>
                <input type="text" id="gName" placeholder="Guardian Name" style="padding: 5px;">
                <input type="text" id="gContact" placeholder="Guardian Contact" style="padding: 5px;"><br><br>
                <button onclick="enrollStudent()" style="padding: 5px;">Enroll Student</button>
            </div>

            <div style="background: #d4edda; padding: 15px; margin-bottom: 10px;">
                <h3>3. View Roster</h3>
                <button onclick="fetchStudents()" style="padding: 5px;">Get All Students</button>
            </div>
            
            <div style="background: #ffeeba; padding: 15px; margin-bottom: 10px;">
                <h3>4. Update & Delete (Needs Student ID)</h3>
                <input type="number" id="sId" placeholder="Student ID (e.g., 1)" style="padding: 5px; margin-bottom: 10px;"><br>
                
                <input type="text" id="newContact" placeholder="New Phone Number" style="padding: 5px;">
                <button onclick="updateStudent()" style="padding: 5px;">Update Phone</button><br><br>
                
                <button onclick="deleteStudent()" style="padding: 5px; background: #ffcccc;">Delete Student</button>
            </div>

            <pre id="output" style="background: #333; color: #0f0; padding: 15px; margin-top: 20px; white-space: pre-wrap;"></pre>

            <script>
                async function sendAuth(endpoint) {
                    const email = document.getElementById('email').value;
                    const password = document.getElementById('pass').value;
                    const res = await fetch(endpoint, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({email, password})
                    });
                    document.getElementById('output').innerText = await res.text();
                }
                async function enrollStudent() {
                    const first_name = document.getElementById('fName').value;
                    const last_name = document.getElementById('lName').value;
                    const guardian_name = document.getElementById('gName').value;
                    const guardian_contact = document.getElementById('gContact').value;
                    const res = await fetch('/api/enroll_student', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({first_name, last_name, guardian_name, guardian_contact})
                    });
                    document.getElementById('output').innerText = await res.text();
                }
                async function fetchStudents() {
                    const res = await fetch('/api/students');
                    try {
                        const data = await res.json();
                        document.getElementById('output').innerText = JSON.stringify(data, null, 4); 
                    } catch (e) {
                        document.getElementById('output').innerText = await res.text();
                    }
                }
                async function updateStudent() {
                    const id = document.getElementById('sId').value;
                    const contact = document.getElementById('newContact').value;
                    const res = await fetch('/api/students/' + id, {
                        method: 'PUT',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({guardian_contact: contact})
                    });
                    document.getElementById('output').innerText = await res.text();
                }
                async function deleteStudent() {
                    const id = document.getElementById('sId').value;
                    const res = await fetch('/api/students/' + id, { method: 'DELETE' });
                    document.getElementById('output').innerText = await res.text();
                }
            </script>
        </body>
    </html>
    """

if __name__ == '__main__':
    app.run(debug=True)
