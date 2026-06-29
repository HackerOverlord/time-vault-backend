# ⭐🔐 Time Vault

A secure, encrypted digital vault for preserving memories, messages, and multimedia content with timed release mechanisms.

## ✨ Features

### 🔒 Secure Foundation (The "Vault")
- **Time-based Release**: Set specific release dates for memories
- **Encryption at Rest**: All content encrypted using Fernet symmetric encryption
- **Access Control**: Only creators can view locked capsules
- **Database Security**: Encrypted data stored in SQLAlchemy database

### 🎥 Multimedia & Storage (The Storytelling Engine)
- **File Upload**: Support for images and videos with UUID naming
- **Secure Storage**: Files stored in protected directory
- **Access Validation**: Release date checked before serving media
- **Story Prompts**: Rotating questions to inspire autobiographical content

### 🛡️ Reliability Logic (Dead Man's Switch)
- **Inactive Detection**: Monitors user login activity
- **Trustee System**: Designate emergency access contacts
- **Automated Workflow**: Verification emails for inactive users
- **Emergency Access**: Trustee can access release-on-death memories

### 🔐 Security Hardening
- **Encryption**: cryptography.fernet for data protection
- **Countdown Timers**: Real-time display until release
- **Transmission Feature**: Designated recipients for future delivery
- **Secure Streaming**: 403 errors for locked content access

## 🚀 Quick Start

### Prerequisites
- Python 3.8+
- pip package manager

### Installation

1. **Clone and Setup**
   ```bash
   cd time_capsule
   pip install -r requirements.txt
   ```

2. **Environment Configuration**
   ```bash
   # Set encryption key (generate new one for production)
   export ENCRYPTION_KEY="your-fernet-key-here"
   ```

3. **Initialize Database**
   ```bash
   python run.py
   ```

4. **Access Application**
   - Frontend: http://localhost:5000
   - API: http://localhost:5000/api

## 📁 Project Structure

```
time_capsule/
├── app.py              # Main Flask application with API routes
├── run.py              # Application entry point with auth routes
├── requirements.txt      # Python dependencies
├── templates/           # HTML templates
│   ├── index.html       # Main dashboard
│   ├── login.html       # User login
│   └── register.html    # User registration
├── protected_media/      # Secure file storage (auto-created)
└── time_capsule.db     # SQLite database (auto-created)
```

## 🔧 API Endpoints

### Authentication
- `POST /api/register` - Create new user account
- `POST /api/login` - User authentication
- `GET /api/user` - Get current user info

### Memory Management
- `GET /api/memories` - List all user memories
- `POST /api/memories` - Create new memory capsule
- `GET /api/memories/<id>` - Get specific memory (if released)

### File Operations
- `POST /api/upload` - Upload multimedia files
- `GET /api/media/<filename>` - Stream media (with release validation)

### Trustee Access
- `POST /api/trustee-access` - Emergency access for inactive users

## 🛡️ Security Features

### Encryption
- **Content Encryption**: All memory content encrypted at rest
- **File Path Encryption**: Media file paths encrypted in database
- **Key Management**: Fernet symmetric encryption with environment key

### Access Control
- **Release Date Validation**: Backend checks current_time vs release_date
- **User Authentication**: Session-based access control
- **File Protection**: 403 errors for locked media access

### Dead Man's Switch
- **Inactivity Detection**: 6-month threshold triggers workflow
- **Trustee Verification**: Email verification before access granted
- **Emergency Protocol**: Release-on-death memories accessible to trustees

## 🎨 Frontend Features

### Dashboard
- **Active Capsules**: Locked memories with countdown timers
- **Opened Memories**: Released content gallery
- **Minimalist Design**: Clean, modern dark theme
- **Responsive Layout**: Mobile-friendly interface

### Interactive Elements
- **Real-time Countdowns**: JavaScript timers for each capsule
- **Media Indicators**: Visual icons for video/image content
- **Story Prompts**: Rotating questions to inspire content
- **Audio Recording**: Browser-based story recording capability

### User Experience
- **Modal Windows**: Clean popup interfaces
- **Form Validation**: Client-side input checking
- **Status Messages**: Success/error notifications
- **Smooth Animations**: CSS transitions and hover effects

## 🔒 Security Best Practices

### Production Deployment
1. **Environment Variables**
   ```bash
   export ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
   export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
   ```

2. **Database Security**
   - Use PostgreSQL/MySQL for production
   - Enable database encryption
   - Regular backups with encryption

3. **File Storage**
   - Use cloud storage with encryption
   - Implement CDN for media delivery
   - Set up proper file permissions

### Access Controls
- **Session Management**: Secure session configuration
- **Rate Limiting**: Prevent brute force attacks
- **Input Validation**: Sanitize all user inputs
- **HTTPS Only**: SSL/TLS certificates required

## 🚀 Usage Examples

### Creating a Memory Capsule
```javascript
const memoryData = {
    title: "My First Time Capsule",
    content: "Dear future self...",
    release_date: "2030-01-01T00:00:00",
    release_on_death: false,
    transmission_emails: ["friend@example.com"]
};

axios.post('/api/memories', memoryData)
  .then(response => console.log('Capsule created'))
  .catch(error => console.error('Creation failed'));
```

### Uploading Media
```javascript
const formData = new FormData();
formData.append('file', videoFile);
formData.append('memory_id', memoryId);

axios.post('/api/upload', formData, {
  headers: { 'Content-Type': 'multipart/form-data' }
});
```

### Trustee Emergency Access
```javascript
const trusteeData = {
  trustee_email: "trustee@example.com",
  trustee_name: "John Doe"
};

axios.post('/api/trustee-access', trusteeData)
  .then(response => console.log('Emergency access granted'))
  .catch(error => console.error('Access denied'));
```

## 🔄 Background Tasks

### Inactive User Monitoring
- **Daily Check**: Scheduler runs user inactivity check
- **Email Notifications**: Verification emails sent to inactive users
- **Grace Period**: 6-month threshold before trustee access
- **Automated Cleanup**: Remove expired sessions

### Release Date Processing
- **Real-time Validation**: Check current time vs release date
- **Status Updates**: Mark memories as released when accessed
- **Media Access**: Validate release dates before file streaming

## 🧪 Testing

### Security Testing
```bash
# Test encryption/decryption
python -c "
from app import encrypt_data, decrypt_data
encrypted = encrypt_data('test message')
decrypted = decrypt_data(encrypted)
print(f'Original: test message')
print(f'Decrypted: {decrypted}')
"
```

### API Testing
```bash
# Test memory creation
curl -X POST http://localhost:5000/api/memories \
  -H "Content-Type: application/json" \
  -d '{"title":"Test","content":"Test content","release_date":"2024-12-31T00:00:00"}'
```

## 📊 Database Schema

### Users Table
```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email VARCHAR(120) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_login DATETIME,
    trustee_email VARCHAR(120),
    trustee_name VARCHAR(120),
    is_active BOOLEAN DEFAULT TRUE
);
```

### Memories Table
```sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    title VARCHAR(200) NOT NULL,
    content_encrypted BLOB NOT NULL,
    video_path_encrypted BLOB,
    image_path_encrypted BLOB,
    release_date DATETIME NOT NULL,
    is_released BOOLEAN DEFAULT FALSE,
    release_on_death BOOLEAN DEFAULT FALSE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    transmission_emails TEXT
);
```

## 🚨 Important Notes

### Security Considerations
- **Key Management**: Store encryption keys securely
- **Backup Strategy**: Regular encrypted backups
- **Access Logs**: Monitor all access attempts
- **Update Dependencies**: Keep packages updated

### Limitations
- **File Size**: 100MB upload limit (configurable)
- **Supported Formats**: Images (PNG, JPG, GIF) and Videos (MP4, MOV, AVI)
- **Browser Support**: Modern browsers with File API support

## 📝 Development

### Adding Features
1. **New API Routes**: Add to `app.py`
2. **Frontend Components**: Modify templates
3. **Database Models**: Update SQLAlchemy models
4. **Security**: Always encrypt sensitive data

### Contributing
1. Fork the repository
2. Create feature branch
3. Implement with tests
4. Submit pull request

## 📄 License

This project is licensed under the MIT License - see LICENSE file for details.

---

**⭐🔐 Time Vault** - Secure your memories for the future, with confidence that they'll remain private until the intended release time.
