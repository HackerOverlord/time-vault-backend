from dotenv import load_dotenv
load_dotenv()
from flask import Flask, request, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime
import os
import secrets
import base64
import random
import string
from datetime import timedelta
from sqlalchemy import or_
import jwt                          
from functools import wraps  
from flask import g

app = Flask(__name__)

# --- INTEGRATION: THE BRIDGE ---
# Allows your v0 frontend to securely request data from this Flask backend
import re

CORS(app, supports_credentials=True, origins=[
    "http://localhost:3000",
    "https://time-vault-imhg.vercel.app",
    re.compile(r"https://time-vault-imhg.*\.vercel\.app"),
], allow_headers=["Content-Type", "Authorization"], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])

# Add these configurations to ensure cookies are handled correctly over local dev
# Ensure this is also set for security
app.config.update(
    SESSION_COOKIE_SAMESITE='None',
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///time_capsule.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'protected_media'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=1)

db = SQLAlchemy(app)




def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({'error': 'Unauthorized'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            g.user_id = data['user_id']  # ← use g instead of session
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expired'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

# --- SECURITY & ENCRYPTION ---
ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY')
if not ENCRYPTION_KEY:
    raise ValueError("ENCRYPTION_KEY environment variable is not set")

def encrypt_content(content):
    if not content: return b''
    key_bytes = ENCRYPTION_KEY.encode()
    content_bytes = content.encode()
    encrypted = bytearray()
    for i, byte in enumerate(content_bytes):
        encrypted.append(byte ^ key_bytes[i % len(key_bytes)])
    return base64.b64encode(encrypted)

def decrypt_content(encrypted_content):
    if not encrypted_content: return ''
    try:
        key_bytes = ENCRYPTION_KEY.encode()
        encrypted_bytes = base64.b64decode(encrypted_content)
        decrypted = bytearray()
        for i, byte in enumerate(encrypted_bytes):
            decrypted.append(byte ^ key_bytes[i % len(key_bytes)])
        return decrypted.decode()
    except Exception:
        return str(encrypted_content)
    

def generate_lineage_code():
    digits = ''.join(random.choices(string.digits, k=4))
    letters = ''.join(random.choices(string.ascii_uppercase, k=2))
    return f"FAM-{digits}-{letters}"

def ensure_user_has_family(user):
    if user.family_id is None:
        family = Family(lineage_code=generate_lineage_code())
        db.session.add(family)
        db.session.flush()  # so family.id is available before commit
        user.family_id = family.id
        db.session.commit()
    return user.family_id


def create_notification(user_id, type, message):
    notif = Notification(user_id=user_id, type=type, message=message)
    db.session.add(notif)
    # No commit here — caller commits


def get_mime_type(filename):
    if not filename:
        return 'data:application/octet-stream'
    ext = filename.lower().split('.')[-1]
    mime_types = {
        'pdf': 'data:application/pdf',
        'jpg': 'data:image/jpeg',
        'jpeg': 'data:image/jpeg',
        'png': 'data:image/png',
        'gif': 'data:image/gif',
        'txt': 'data:text/plain',
        'doc': 'data:application/msword',
    }
    return mime_types.get(ext, 'data:application/octet-stream')

# --- DATABASE MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(60), nullable=True)
    last_name = db.Column(db.String(60), nullable=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    last_login = db.Column(db.DateTime)
    family_id = db.Column(db.Integer, db.ForeignKey('family.id'), nullable=True)
    avatar = db.Column(db.Text, nullable=True)  # stores base64


class Family(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    lineage_code = db.Column(db.String(20), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class InviteCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    family_id = db.Column(db.Integer, db.ForeignKey('family.id'), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False)



class FamilyMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    family_id = db.Column(db.Integer, db.ForeignKey('family.id'), nullable=True)
    name = db.Column(db.String(100), nullable=False)
    first_name = db.Column(db.String(50))
    last_name = db.Column(db.String(50))
    suffix = db.Column(db.String(10))
    email = db.Column(db.String(120))
    description = db.Column(db.Text)
    photo = db.Column(db.Text)  # Stores the base64 string from profilePreview
    parent_id = db.Column(db.Integer, db.ForeignKey('family_member.id'), nullable=True)
    
    # Store milestones as a JSON blob or a separate table
    milestones = db.Column(db.JSON)
    
    bio_attachments = db.Column(db.JSON)

class Memory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    content_encrypted = db.Column(db.LargeBinary, nullable=False)
    media_content = db.Column(db.LargeBinary, nullable=True)  # ADD THIS LINE
    media_type = db.Column(db.String(50), nullable=True)  # ADD THIS LINE - 'video' or 'audio'
    attachments = db.Column(db.JSON, nullable=True)
    release_date = db.Column(db.DateTime, nullable=False)
    is_released = db.Column(db.Boolean, default=False)
    is_draft = db.Column(db.Boolean, default=False)
    recipient_email = db.Column(db.String(120), nullable=True)
    hidden_from_sender = db.Column(db.Boolean, default=False)
    hidden_from_recipient = db.Column(db.Boolean, default=False)

    @property
    def content(self): return decrypt_content(self.content_encrypted)
    @content.setter
    def content(self, value): self.content_encrypted = encrypt_content(value)

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    type = db.Column(db.String(50), nullable=False)  # 'vault_sent', 'vault_received', 'vault_deleted'
    message = db.Column(db.String(255), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)




# --- API ROUTES FOR V0 ---


@app.route('/api/family-members', methods=['POST'])
@token_required
def add_member():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    email_val = data.get('email')
    

    user = User.query.get(g.user_id)
    ensure_user_has_family(user)

    new_member = FamilyMember(
        user_id=g.user_id,
        family_id=user.family_id,
        name=data.get('name'),
        first_name=data.get('firstName'),
        last_name=data.get('lastName'),
        suffix=data.get('suffix'),
        photo=data.get('photo'),
        parent_id=data.get('parentId'),
        milestones=data.get('milestones'),
        bio_attachments=data.get('bioAttachments'),
        email=email_val,
        description=data.get('description', ''),
    )
    
    db.session.add(new_member)
    db.session.commit()
    create_notification(g.user_id, 'member_added',
        f"Family member \"{new_member.first_name or new_member.name}\" was added successfully.")
    db.session.commit()
    
    # === VERIFY DATABASE LINK BEFORE RETURNING RESPONSE ===
    is_user_registered = User.query.filter_by(email=email_val).first() is not None if email_val else False
    
    return jsonify({
        "id": new_member.id,
        "name": new_member.name,
        "firstName": new_member.first_name,
        "lastName": new_member.last_name,
        "suffix": new_member.suffix,
        "email": new_member.email,
        "linkedAccount": new_member.email if is_user_registered else None,  # Fix here
        "photo": new_member.photo,
        "parentId": new_member.parent_id,
        "milestones": new_member.milestones,
        "bioAttachments": new_member.bio_attachments
    }), 201


@app.route('/api/family-members/<int:member_id>', methods=['DELETE'])
@token_required
def delete_member(member_id):
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401

    member = FamilyMember.query.get_or_404(member_id)



    user = User.query.get(g.user_id)
    if member.family_id != user.family_id:
        return jsonify({'error': 'Forbidden'}), 403

    try:
        # Disconnect any children so they don't have a 'ghost' parent
        children = FamilyMember.query.filter_by(parent_id=member_id).all()
        for child in children:
            child.parent_id = None
        
        db.session.delete(member)
        db.session.commit()
        return jsonify({"message": "Member deleted successfully"}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@app.route('/api/stats', methods=['GET'])
@token_required
def get_dashboard_stats():
    """Feeds data to the v0 Dashboard cards"""
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    u_id = g.user_id
    user = User.query.get(u_id)
    return jsonify({
        "totalMemories": Memory.query.filter_by(user_id=u_id, hidden_from_sender=False).count(),
        "lockedVaults": Memory.query.filter_by(user_id=u_id, is_released=False, hidden_from_sender=False).count(),
        "familyMembers": FamilyMember.query.filter_by(family_id=user.family_id).count() if user.family_id else 0
    })

@app.route('/api/family-members', methods=['GET'])
@token_required
def get_family_members():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Not logged in'}), 401
    
    user = User.query.get(g.user_id)
    members = FamilyMember.query.filter_by(family_id=user.family_id).all() if user.family_id else []
    result = []
    
    for member in members:
        # Safe string splitting to prevent crashes for single names (e.g., "Mom")
        name_parts = member.name.split() if member.name else []
        first_name = name_parts[0] if len(name_parts) > 0 else ""
        last_name = name_parts[1] if len(name_parts) > 1 else ""


        is_user_registered = User.query.filter_by(email=member.email).first() is not None if member.email else False

        result.append({
            "id": member.id,
            "name": member.name,
            "firstName": first_name,
            "lastName": last_name,
            "suffix": member.suffix,
            "email": member.email,
            "linkedAccount": member.email if is_user_registered else None,
            "description": member.description,
            
            "photo": member.photo,
            "parentId": member.parent_id,
            "milestones": member.milestones or [],
            "bioAttachments": member.bio_attachments or []
        })
        
    return jsonify(result), 200

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    user = User.query.filter_by(email=email).first()
    if user and check_password_hash(user.password_hash, password):
        token = jwt.encode({
            'user_id': user.id,
            'exp': datetime.utcnow() + timedelta(days=7)
        }, app.config['SECRET_KEY'], algorithm='HS256')
        return jsonify({"message": "Login successful", "token": token}), 200
    return jsonify({"error": "Access Denied"}), 401

@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json()
    
    # Validation: Ensure email and password are provided
    if not data.get('email') or not data.get('password'):
        return jsonify({'error': 'Email and password are required'}), 400

    # Check if user already exists
    if User.query.filter_by(email=data['email']).first():
        return jsonify({'error': 'Email already registered'}), 400

    user = User(
        name=data.get('name'),
        email=data['email'],
        password_hash=generate_password_hash(data['password'])
    )
    
    db.session.add(user)
    db.session.commit()
    token = jwt.encode({
        'user_id': user.id,
        'exp': datetime.utcnow() + timedelta(days=7)
    }, app.config['SECRET_KEY'], algorithm='HS256')
    return jsonify({'message': 'User created', 'token': token}), 201

@app.route('/api/memories', methods=['POST'])
@token_required
def create_memory():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.get_json()
    
    # Clean up the ISO string so python parses it perfectly
    date_str = data.get('releaseDate').replace('Z', '')
    parsed_date = datetime.fromisoformat(date_str)

    # Convert media from base64 string to binary if it exists
    media_content = None
    media_type = None

    if data.get('mediaContent'):
        import base64
        # Extract the base64 part and media type
        media_data = data.get('mediaContent')
        if media_data.startswith('data:'):
            # Format: data:video/webm;base64,xxxxx
            media_type = media_data.split(':')[1].split(';')[0]  # Extract 'video/webm'
            media_content = base64.b64decode(media_data.split(',')[1])

    # Create a new memory
    # Encrypt the content before storing
    import base64
    content_to_encrypt = data.get('content', '')
    key_bytes = ENCRYPTION_KEY.encode()
    content_bytes = content_to_encrypt.encode()
    encrypted_bytes = bytearray()
    for i, byte in enumerate(content_bytes):
        encrypted_bytes.append(byte ^ key_bytes[i % len(key_bytes)])
    encrypted_content = base64.b64encode(encrypted_bytes).decode()

    new_memory = Memory(
        user_id=g.user_id,
        title=data.get('title'),
        content_encrypted=encrypted_content.encode(),  # Store encrypted as binary
        media_content=media_content,
        media_type=media_type,
        release_date=parsed_date,
        is_released=False,
        is_draft=data.get('is_draft', False),
        recipient_email=data.get('recipient')
    )

    attachments_list = []
    if data.get('attachments'):
        import base64
        for attachment in data.get('attachments'):
            if isinstance(attachment, dict):
                image_data = attachment.get('data')
                image_name = attachment.get('name')
            else:
                image_data = attachment
                image_name = None
                
            if image_data:
                if isinstance(image_data, str) and image_data.startswith('data:'):
                    image_data = image_data.split(',')[1]
                
                attachments_list.append({
                    'name': image_name,
                    'data': image_data
                })

    new_memory.attachments = attachments_list if attachments_list else None
    
    db.session.add(new_memory)
    db.session.flush()  # get new_memory.id before commit

    if not new_memory.is_draft and new_memory.recipient_email:
        # Notify sender
        create_notification(g.user_id, 'vault_sent',
            f"You sent \"{new_memory.title}\" to {new_memory.recipient_email}.")
        # Notify recipient if they have an account
        recipient_user = User.query.filter_by(email=new_memory.recipient_email).first()
        if recipient_user:
            create_notification(recipient_user.id, 'vault_received',
                f"You received a new vault: \"{new_memory.title}\".")

    db.session.commit()
    return jsonify({
    'message': 'Vault created successfully',
    'is_draft': new_memory.is_draft,
    'has_recipient': bool(new_memory.recipient_email)
    }), 201

@app.route('/api/memories/shared', methods=['GET'])
@token_required
def get_shared_memories():
    """Fetches all capsules addressed to the currently logged-in family recipient account"""
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
        
    current_user = User.query.get(g.user_id)
    if not current_user:
        return jsonify({'error': 'User profile not found'}), 404
        
    now = datetime.now()

    # REMOVED the '<= now' constraint so the recipient can see the countdown!
    shared_memories = Memory.query.filter(
    Memory.recipient_email == current_user.email,
    Memory.is_draft == False,
    Memory.hidden_from_recipient == False
    ).all()
    
    result = []
    for m in shared_memories:
        # Convert binary media to base64 if it exists
        media_content = None
        if m.media_content:
            import base64
            media_content = 'data:' + m.media_type + ';base64,' + base64.b64encode(m.media_content).decode()
        
        image_content = None
        if m.attachments:
            import base64
        
        result.append({
            "id": m.id,
            "title": m.title,
            "content": m.content if m.release_date <= now else "",
            "mediaContent": media_content,
            "attachments": [],  # Add attachments handling
            "hasImage": False,
            "hasVideo": m.media_type == 'video/webm' if m.media_type else False,
            "hasAudio": m.media_type == 'audio/webm' if m.media_type else False,
            "release_date": m.release_date.isoformat(),
            "status": "released" if m.release_date <= now else "locked",
            "is_draft": False,
            "recipient": m.recipient_email,
            "sender": User.query.get(m.user_id).name if User.query.get(m.user_id) else "Unknown",

        })

    return jsonify(result), 200

@app.route('/api/heartbeat', methods=['POST'])
@token_required
def heartbeat():
    """Updates last_login to prevent Dead Man's Switch trigger"""
    if 'user_id' in session:
        user = User.query.get(g.user_id)
        user.last_login = datetime.utcnow()
        db.session.commit()
        return jsonify({'status': 'active'}), 200
    return jsonify({'status': 'guest'}), 200


@app.route('/api/memories', methods=['GET'])
@token_required
def get_memories():
    """Returns the list of time-locked vaults for the dashboard"""
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    u_id = g.user_id
    memories = Memory.query.filter_by(user_id=u_id, hidden_from_sender=False).all()
    
    return jsonify([{
        "id": m.id,
        "title": m.title,
        "release_date": m.release_date.isoformat(),
        "status": "released" if m.is_released else "locked",
        "is_draft": m.is_draft 
    } for m in memories])




@app.route('/api/get-my-code', methods=['GET'])
@token_required
def get_my_code():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401

    user = User.query.get(g.user_id)
    ensure_user_has_family(user)
    family = Family.query.get(user.family_id)

    chars = string.ascii_uppercase + string.digits
    code = ''.join(random.choices(chars, k=6))
    formatted_code = f"{code[:3]}-{code[3:]}"

    invite = InviteCode(
        code=formatted_code,
        family_id=family.id,
        created_by=user.id,
        expires_at=datetime.utcnow() + timedelta(hours=24)
    )
    db.session.add(invite)
    db.session.commit()

    return jsonify({
        "invite_code": formatted_code,
    }), 200


@app.route('/api/join-family', methods=['POST'])
@token_required
def join_family():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    code = (data.get('invite_code') or '').strip().upper()


    print(f"[JOIN] user_id={g.user_id} code={code}")


    invite = InviteCode.query.filter_by(code=code, used=False).first()
    if not invite:
        return jsonify({'message': 'Invalid or expired invite code.'}), 400
    if invite.expires_at < datetime.utcnow():
        return jsonify({'message': 'This invite code has expired.'}), 400

    user = User.query.get(g.user_id)

    print(f"[JOIN] invite.created_by={invite.created_by} joining user.id={user.id} same={invite.created_by == user.id}")

    if invite.created_by == user.id:
        return jsonify({'message': "You can't join your own family via invite."}), 400

    user.family_id = invite.family_id
    invite.used = True
    db.session.commit()
    create_notification(g.user_id, 'family_joined',
        f"You successfully joined a family using an invite code.")
    # Also notify the invite creator
    create_notification(invite.created_by, 'member_joined',
        f"{user.name} joined your family using an invite code.")
    db.session.commit()

    return jsonify({'message': 'Successfully joined family!'}), 200



@app.route('/api/family-members/<int:member_id>', methods=['PUT'])
@token_required
def update_member(member_id):

    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401

    member = FamilyMember.query.get_or_404(member_id)
    data = request.get_json()
    
    user = User.query.get(g.user_id)
    # AFTER
    if member.family_id != user.family_id and member.user_id != g.user_id:
        return jsonify({'error': 'Forbidden'}), 403
    
    member.first_name = data.get('firstName', member.first_name)
    member.last_name = data.get('lastName', member.last_name)
    member.suffix = data.get('suffix', member.suffix)
    member.name = data.get('name', member.name)
    incoming_email = data.get('linkedAccount') or data.get('email')
    
    if incoming_email is not None:
        member.email = incoming_email

    member.photo = data.get('photo', member.photo)
    member.milestones = data.get('milestones', member.milestones)
    member.bio_attachments = data.get('bioAttachments', member.bio_attachments)

    if 'description' in data:
        member.description = data.get('description')
    
    
    db.session.commit()
    
    # Return the full object so the frontend state updates correctly
    return jsonify({
        "id": member.id,
        "name": member.name,
        "firstName": member.first_name,
        "lastName": member.last_name,
        "suffix": member.suffix,
        "email": member.email,
        "linkedAccount": member.email if (member.email and hasattr(member, 'is_linked') and member.is_linked) else None,
        "description": member.description,
        "photo": member.photo,
        "parentId": member.parent_id,
        "milestones": member.milestones,
        "bioAttachments": member.bio_attachments
    }), 200


@app.route('/api/memories/<int:memory_id>', methods=['PUT'])
@token_required
def update_memory(memory_id):
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    memory = Memory.query.get_or_404(memory_id)
    
    if memory.user_id != g.user_id:
        return jsonify({'error': 'Forbidden'}), 403
    
    data = request.get_json()
    
    # Update fields
    memory.title = data.get('title', memory.title)
    memory.content_encrypted = encrypt_content(data.get('content', ''))
    memory.release_date = datetime.fromisoformat(data.get('releaseDate').replace('Z', ''))
    memory.is_draft = data.get('is_draft', memory.is_draft)
    memory.recipient_email = data.get('recipient')
    

    # Handle media content
    if data.get('mediaContent'):
        media_data = data.get('mediaContent')
        if ',' in media_data:
            header, b64data = media_data.split(',', 1)
            mime = header.split(':')[1].split(';')[0]
            memory.media_content = base64.b64decode(b64data)
            memory.media_type = mime

    # Handle attachments similarly to POST
    if data.get('attachments'):
        attachments_list = []
        for attachment in data.get('attachments'):
            if isinstance(attachment, dict):
                attachments_list.append({
                    'name': attachment.get('name'),
                    'data': attachment.get('data').split(',')[1] if 'data:' in attachment.get('data', '') else attachment.get('data')
                })
        memory.attachments = attachments_list if attachments_list else None
    
    db.session.commit()
    return jsonify({'message': 'Vault updated successfully'}), 200

@app.route('/api/me', methods=['GET'])
@token_required
def get_current_user():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Not logged in'}), 401
    

    user = User.query.get(g.user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
        
    return jsonify({
    "name": user.name,
    "firstName": user.first_name or user.name.split()[0],
    "lastName": user.last_name or (user.name.split()[1] if len(user.name.split()) > 1 else ''),
    "email": user.email,
    "avatar": user.avatar or None
}), 200


@app.route('/api/check-email', methods=['POST'])
def check_email():
    data = request.get_json()
    email = data.get('email')
    
    if not email:
        return jsonify({"exists": False}), 400

    # This checks your User database table
    user = User.query.filter_by(email=email).first()
    
    return jsonify({"exists": user is not None})


@app.route('/api/memories/<int:memory_id>', methods=['GET'])
@token_required
def get_single_memory(memory_id):
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
        
    memory = Memory.query.get_or_404(memory_id)
    current_user = User.query.get(g.user_id)
    
    # Allow access if user is owner OR recipient
    is_owner = memory.user_id == g.user_id
    is_recipient = current_user and memory.recipient_email == current_user.email
    
    if not is_owner and not is_recipient:
        return jsonify({'error': 'Forbidden'}), 403
    
    # Convert binary media to base64 if it exists
    media_content = None
    if memory.media_content:
        import base64
        media_content = 'data:' + memory.media_type + ';base64,' + base64.b64encode(memory.media_content).decode()
        
    attachments = []
    if memory.attachments:
        for att in memory.attachments:
            attachments.append({
                'name': att.get('name'),
                'content': get_mime_type(att.get('name')) + ';base64,' + att.get('data')
            })
    
    return jsonify({
        "id": memory.id,
        "title": memory.title,
        "content": memory.content,
        "mediaContent": media_content,  # ADD THIS
        "attachments": attachments,
        "hasImage": len(attachments) > 0,
        "hasVideo": memory.media_type == 'video/webm' if memory.media_type else False, 
        "hasAudio": memory.media_type == 'audio/webm' if memory.media_type else False,
        "release_date": memory.release_date.isoformat(),
        "status": "released" if memory.is_released else "locked",
        "is_draft": memory.is_draft
    }), 200


@app.route('/api/vaults/<int:memory_id>', methods=['DELETE'])
@token_required
def delete_vault(memory_id):
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401

    memory = Memory.query.get_or_404(memory_id)
    current_user = User.query.get(g.user_id)

    is_owner = memory.user_id == g.user_id
    is_recipient = current_user and memory.recipient_email == current_user.email

    if not is_owner and not is_recipient:
        return jsonify({'error': 'Forbidden'}), 403

    try:
        if is_recipient:
            memory.hidden_from_recipient = True
            create_notification(g.user_id, 'vault_deleted',
                f"You removed \"{memory.title}\" from your inbox.")
            db.session.commit()
            return jsonify({"message": "Removed from your inbox."}), 200
        else:
            was_sent = bool(memory.recipient_email) and not memory.is_draft
            if was_sent:
                memory.hidden_from_sender = True
                if memory.status == 'locked':  # not yet released — hide from recipient too
                    memory.hidden_from_recipient = True
                create_notification(g.user_id, 'vault_deleted',
                    f"You removed \"{memory.title}\" from your vault.")
                db.session.commit()
                return jsonify({"message": "Vault removed from your view."}), 200
            else:
                title = memory.title
                db.session.delete(memory)
                create_notification(g.user_id, 'vault_deleted',
                    f"You permanently deleted \"{title}\".")
                db.session.commit()
                return jsonify({"message": "Vault deleted."}), 200                           
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@app.route('/api/me', methods=['PUT'])
@token_required
def update_current_user():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    
    user = User.query.get(g.user_id)
    data = request.get_json()
    
    if data.get('avatar'):
        user.avatar = data.get('avatar')

    user.first_name = data.get('firstName', user.first_name)
    user.last_name = data.get('lastName', user.last_name)
    user.name = f"{user.first_name} {user.last_name}".strip()
    
    db.session.commit()
    return jsonify({'message': 'Profile updated successfully'}), 200


@app.route('/api/notifications', methods=['GET'])
@token_required
def get_notifications():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    notifs = Notification.query.filter_by(user_id=g.user_id)\
        .order_by(Notification.created_at.desc()).limit(50).all()
    return jsonify([{
        'id': n.id,
        'type': n.type,
        'message': n.message,
        'is_read': n.is_read,
        'created_at': n.created_at.isoformat()
    } for n in notifs]), 200

@app.route('/api/notifications/read/<int:notif_id>', methods=['POST'])
@token_required
def mark_notification_read(notif_id):
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    notif = Notification.query.get_or_404(notif_id)
    if notif.user_id != g.user_id:
        return jsonify({'error': 'Forbidden'}), 403
    notif.is_read = True
    db.session.commit()
    return jsonify({'message': 'Marked read'}), 200

@app.route('/api/notifications/read-all', methods=['POST'])
@token_required
def mark_all_read():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    Notification.query.filter_by(user_id=g.user_id, is_read=False)\
        .update({'is_read': True})
    db.session.commit()
    return jsonify({'message': 'All marked read'}), 200

@app.route('/api/leave-family', methods=['POST'])
@token_required
def leave_family():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    user = User.query.get(g.user_id)
    if not user.family_id:
        return jsonify({'error': 'Not in a family'}), 400
    
    # Give them their own new solo family
    old_family_id = user.family_id
    new_family = Family(lineage_code=generate_lineage_code())
    db.session.add(new_family)
    db.session.flush()
    user.family_id = new_family.id

    # Move user's own family members to the new family
    FamilyMember.query.filter_by(
        family_id=old_family_id,
        user_id=user.id
    ).update({'family_id': new_family.id})
    
    create_notification(g.user_id, 'family_left',
        "You've left the shared family. Your data remains intact.")
    db.session.commit()
    return jsonify({'message': 'Left family successfully'}), 200


@app.route('/api/family-status', methods=['GET'])
@token_required
def family_status():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    user = User.query.get(g.user_id)
    # Count how many users share this family_id
    member_count = User.query.filter_by(family_id=user.family_id).count()
    return jsonify({'in_shared_family': member_count > 1}), 200

@app.route('/api/change-password', methods=['POST'])
@token_required
def change_password():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    user = User.query.get(g.user_id)
    if not check_password_hash(user.password_hash, data.get('current_password', '')):
        return jsonify({'error': 'Current password is incorrect'}), 400
    if len(data.get('new_password', '')) < 8:
        return jsonify({'error': 'New password must be at least 8 characters'}), 400
    user.password_hash = generate_password_hash(data['new_password'])
    db.session.commit()
    return jsonify({'message': 'Password updated successfully'}), 200

@app.route('/api/delete-account', methods=['DELETE'])
@token_required
def delete_account():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    user = User.query.get(g.user_id)
    if not check_password_hash(user.password_hash, data.get('password', '')):
        return jsonify({'error': 'Incorrect password'}), 400
    # Delete user's data
    Memory.query.filter_by(user_id=user.id).delete()
    FamilyMember.query.filter_by(family_id=user.family_id).delete()
    Notification.query.filter_by(user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()
    session.clear()
    return jsonify({'message': 'Account deleted'}), 200

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=False, port=5000)