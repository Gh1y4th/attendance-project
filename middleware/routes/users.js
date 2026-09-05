// routes/users.js
const express = require('express');
const router = express.Router();
const admin = require('firebase-admin');
const { getDb } = require('../firestore');
const { verifyFirebaseToken, requireDbUser, requireRole } = require('../middleware/auth');

const ALL_ROLES = ['admin', 'school_admin', 'professor', 'family'];

function generateTempPassword() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789!@#$';
  let pwd = '';
  for (let i = 0; i < 12; i++) pwd += chars.charAt(Math.floor(Math.random() * chars.length));
  return pwd;
}

router.get('/', verifyFirebaseToken, requireDbUser, requireRole('admin', 'school_admin'), async (req, res) => {
  const db = getDb();
  const { role, schoolId } = req.dbUser;

  try {
    let snap;
    if (role === 'admin') {
      snap = await db.collection('users').get();
    } else {
      snap = await db.collection('users').where('schoolId', '==', schoolId).get();
    }

    const users = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
    res.json(users);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to load users' });
  }
});

router.post('/', verifyFirebaseToken, requireDbUser, requireRole('admin', 'school_admin'), async (req, res) => {
  const db = getDb();
  const caller = req.dbUser;
  let { email, role, full_name, schoolId } = req.body;

  if (!email || !role || !full_name) {
    return res.status(400).json({ error: 'email, role, and full_name are required' });
  }
  if (!ALL_ROLES.includes(role)) {
    return res.status(400).json({ error: 'Invalid role' });
  }

  if (caller.role === 'school_admin') {
    if (!['professor', 'family'].includes(role)) {
      return res.status(403).json({ error: 'School admins can only create professor or family accounts' });
    }
    schoolId = caller.schoolId;
  } else {
    if (role !== 'admin' && !schoolId) {
      return res.status(400).json({ error: 'schoolId is required for this role' });
    }
  }

  try {
    const existing = await db.collection('users').where('email', '==', email).limit(1).get();
    if (!existing.empty) {
      return res.status(409).json({ error: 'This email is already authorized' });
    }

    const tempPassword = generateTempPassword();
    const userRecord = await admin.auth().createUser({ email, password: tempPassword });

    await db.collection('users').doc(userRecord.uid).set({
      email,
      role,
      full_name,
      schoolId: schoolId || null,
      is_active: true,
      linkedStudentIds: [],
      createdBy: caller.id,
      createdAt: new Date(),
    });

    res.status(201).json({ id: userRecord.uid, tempPassword, success: true });
  } catch (err) {
    console.error(err);
    if (err.code === 'auth/email-already-exists') {
      return res.status(409).json({ error: 'This email already has a login somewhere in Firebase Auth' });
    }
    res.status(500).json({ error: 'Failed to create account' });
  }
});

router.patch('/:id', verifyFirebaseToken, requireDbUser, requireRole('admin', 'school_admin'), async (req, res) => {
  const db = getDb();
  const caller = req.dbUser;
  const targetRef = db.collection('users').doc(req.params.id);
  const targetSnap = await targetRef.get();

  if (!targetSnap.exists) return res.status(404).json({ error: 'User not found' });
  const target = targetSnap.data();

  if (caller.role === 'school_admin') {
    if (!['professor', 'family'].includes(target.role) || target.schoolId !== caller.schoolId) {
      return res.status(403).json({ error: 'You cannot edit this account' });
    }
    if (req.body.role && !['professor', 'family'].includes(req.body.role)) {
      return res.status(403).json({ error: 'School admins cannot promote accounts to admin or school_admin' });
    }
    if (req.body.schoolId && req.body.schoolId !== caller.schoolId) {
      return res.status(403).json({ error: 'Cannot move a user to a different school' });
    }
  }

  const allowedFields = ['role', 'full_name', 'is_active', 'linkedStudentIds', 'schoolId'];
  const updates = {};
  for (const field of allowedFields) {
    if (req.body[field] !== undefined) updates[field] = req.body[field];
  }
  updates.updatedBy = caller.id;
  updates.updatedAt = new Date();

  try {
    await targetRef.update(updates);
    res.json({ success: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to update account' });
  }
});

router.delete('/:id', verifyFirebaseToken, requireDbUser, requireRole('admin', 'school_admin'), async (req, res) => {
  const db = getDb();
  const caller = req.dbUser;
  const targetRef = db.collection('users').doc(req.params.id);
  const targetSnap = await targetRef.get();

  if (!targetSnap.exists) return res.status(404).json({ error: 'User not found' });
  const target = targetSnap.data();

  if (caller.role === 'school_admin') {
    if (!['professor', 'family'].includes(target.role) || target.schoolId !== caller.schoolId) {
      return res.status(403).json({ error: 'You cannot delete this account' });
    }
  }

  try {
    await admin.auth().deleteUser(req.params.id).catch(() => {});
    await targetRef.delete();
    res.json({ success: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to delete account' });
  }
});

module.exports = router;
