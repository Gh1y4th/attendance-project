// routes/schools.js
const express = require('express');
const router = express.Router();
const { getDb } = require('../firestore');
const { verifyFirebaseToken, requireDbUser, requireRole } = require('../middleware/auth');

router.get('/', verifyFirebaseToken, requireDbUser, requireRole('admin', 'school_admin'), async (req, res) => {
  const db = getDb();
  try {
    const snap = await db.collection('schools').get();
    res.json(snap.docs.map((d) => ({ id: d.id, ...d.data() })));
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to load schools' });
  }
});

router.post('/', verifyFirebaseToken, requireDbUser, requireRole('admin'), async (req, res) => {
  const db = getDb();
  const { name } = req.body;
  if (!name) return res.status(400).json({ error: 'name is required' });

  try {
    const docRef = await db.collection('schools').add({ name, createdAt: new Date() });
    res.status(201).json({ id: docRef.id, success: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to create school' });
  }
});

module.exports = router;
