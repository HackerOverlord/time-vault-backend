"""Notifications: list and mark read."""

from flask import Blueprint, jsonify, g

from extensions import db
from models import Notification
from security import token_required
from helpers import LEGACY_NOTIFICATION_TYPES



bp = Blueprint('notifications', __name__)

@bp.route('/api/notifications', methods=['GET'])
@token_required
def get_notifications():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    notifs = Notification.query.filter_by(user_id=g.user_id)\
        .filter(Notification.type.notin_(LEGACY_NOTIFICATION_TYPES))\
        .order_by(Notification.created_at.desc()).limit(50).all()
    return jsonify([{
        'id': str(n.id),
        'type': n.type,
        'message': n.message,
        'is_read': n.is_read,
        'created_at': n.created_at.isoformat(),
        'vault_id': str(n.vault_id) if n.vault_id else None,
    } for n in notifs]), 200

@bp.route('/api/notifications/<int:notif_id>/read', methods=['POST'])
@token_required
def mark_notification_read(notif_id):
    n = Notification.query.get(notif_id)
    if not n:
        return jsonify({'error': 'Notification not found'}), 404
    if n.user_id != g.user_id:
        return jsonify({'error': 'Forbidden'}), 403
    n.is_read = True
    db.session.commit()
    return jsonify({'id': n.id, 'is_read': True}), 200


@bp.route('/api/notifications/read-all', methods=['POST'])
@token_required
def mark_all_read():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    Notification.query.filter_by(user_id=g.user_id, is_read=False)\
        .update({'is_read': True})
    db.session.commit()
    return jsonify({'message': 'All marked read'}), 200
