import os
from flask import Flask, jsonify
from flask_login import LoginManager
from auth_models import User

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

if __name__ == '__main__':
    app.run(debug=True)
