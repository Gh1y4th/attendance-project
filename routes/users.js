const express = require('express');
const router = express.Router();
const admin = require('firebase-admin');
const { getDb } = require('../firestore');
const { verifyFirebaseToken, requireDbUser, requireRole } = require('../middleware/auth');

const ALL_ROLES = ['dev', 'school_admin', 'professor', 'family'];

function generateTempPassword() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789!@#$';
  let pwd = '';
  for (let i = 0; i < 12; i++) pwd += chars.charAt(Math.floor(Math.random() * chars.length));
  return pwd;
}

router.get('/', verifyFirebaseToken, requireDbUser, requireRole('dev'), async (req, res) => {
  const db = getDb();
  try {
    const snap = await db.collection('users').get();
    res.json(snap.docs.map((d) => ({ id: d.id, ...d.data() })));
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to load users' });
  }
});

router.post('/', verifyFirebaseToken, requireDbUser, requireRole('dev'), async (req, res) => {
  const db = getDb();
  const caller = req.dbUser;
  const { email, role, full_name, password } = req.body;

  if (!email || !role || !full_name) return res.status(400).json({ error: 'email, role, and full_name are required' });
  if (!ALL_ROLES.includes(role)) return res.status(400).json({ error: 'Invalid role' });

  try {
    const existing = await db.collection('users').where('email', '==', email).limit(1).get();
    if (!existing.empty) return res.status(409).json({ error: 'This email is already authorized' });

    const usedCustomPassword = !!(password && password.length >= 6);
    const finalPassword = usedCustomPassword ? password : generateTempPassword();
    const userRecord = await admin.auth().createUser({ email, password: finalPassword });

    await db.collection('users').doc(userRecord.uid).set({
      email, role, full_name,
      password: finalPassword,
      is_active: true,
      createdBy: caller.id,
      createdAt: new Date(),
    });

    res.status(201).json({ id: userRecord.uid, tempPassword: finalPassword, success: true });
  } catch (err) {
    console.error(err);
    if (err.code === 'auth/email-already-exists') return res.status(409).json({ error: 'This email already has a login somewhere in Firebase Auth' });
    if (err.code === 'auth/invalid-password') return res.status(400).json({ error: 'Password must be at least 6 characters' });
    res.status(500).json({ error: 'Failed to create account' });
  }
});

router.patch('/:id', verifyFirebaseToken, requireDbUser, requireRole('dev'), async (req, res) => {
  const db = getDb();
  const targetRef = db.collection('users').doc(req.params.id);
  const targetSnap = await targetRef.get();
  if (!targetSnap.exists) return res.status(404).json({ error: 'User not found' });

  const allowedFields = ['role', 'full_name', 'is_active'];
  const updates = {};
  for (const field of allowedFields) if (req.body[field] !== undefined) updates[field] = req.body[field];
  updates.updatedBy = req.dbUser.id;
  updates.updatedAt = new Date();

  try {
    await targetRef.update(updates);
    res.json({ success: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to update account' });
  }
});

router.delete('/:id', verifyFirebaseToken, requireDbUser, requireRole('dev'), async (req, res) => {
  const db = getDb();
  const targetRef = db.collection('users').doc(req.params.id);
  const targetSnap = await targetRef.get();
  if (!targetSnap.exists) return res.status(404).json({ error: 'User not found' });

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
