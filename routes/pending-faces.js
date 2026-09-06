const express = require('express');
const router = express.Router();
const { getDb } = require('../firestore');
const { verifyFirebaseToken, requireDbUser, requireRole, verifyPythonServiceKey } = require('../middleware/auth');

router.post('/', verifyPythonServiceKey, async (req, res) => {
  const { image_base64, camera_id } = req.body;
  if (!image_base64) return res.status(400).json({ error: 'image_base64 is required' });

  const db = getDb();
  try {
    const docRef = await db.collection('pending_faces').add({
      image_base64,
      camera_id: camera_id || null,
      status: 'pending',
      synced: false,
      approved_name: null,
      createdAt: new Date(),
    });
    res.status(201).json({ id: docRef.id, success: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to save pending face' });
  }
});

router.get('/', verifyFirebaseToken, requireDbUser, requireRole('dev', 'school_admin'), async (req, res) => {
  const db = getDb();
  try {
    const snap = await db.collection('pending_faces').where('status', '==', 'pending').get();
    const faces = snap.docs
      .map((d) => ({ id: d.id, ...d.data() }))
      .sort((a, b) => (b.createdAt?._seconds || 0) - (a.createdAt?._seconds || 0));
    res.json(faces);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to load pending faces' });
  }
});

router.post('/:id/approve', verifyFirebaseToken, requireDbUser, requireRole('dev', 'school_admin'), async (req, res) => {
  const db = getDb();
  const { full_name } = req.body;
  if (!full_name) return res.status(400).json({ error: 'full_name is required' });

  try {
    const ref = db.collection('pending_faces').doc(req.params.id);
    const snap = await ref.get();
    if (!snap.exists) return res.status(404).json({ error: 'Pending face not found' });

    await ref.update({
      status: 'approved',
      approved_name: full_name,
      approvedBy: req.dbUser.id,
      approvedAt: new Date(),
    });

    res.json({ success: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to approve' });
  }
});

router.post('/:id/reject', verifyFirebaseToken, requireDbUser, requireRole('dev', 'school_admin'), async (req, res) => {
  const db = getDb();
  try {
    await db.collection('pending_faces').doc(req.params.id).update({
      status: 'rejected',
      rejectedBy: req.dbUser.id,
      rejectedAt: new Date(),
    });
    res.json({ success: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to reject' });
  }
});

router.get('/sync', verifyPythonServiceKey, async (req, res) => {
  const db = getDb();
  try {
    const snap = await db
      .collection('pending_faces')
      .where('status', '==', 'approved')
      .where('synced', '==', false)
      .get();

    const results = snap.docs.map((d) => {
      const data = d.data();
      return { id: d.id, image_base64: data.image_base64, full_name: data.approved_name };
    });

    res.json(results);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to sync' });
  }
});

router.post('/:id/mark-synced', verifyPythonServiceKey, async (req, res) => {
  const db = getDb();
  try {
    await db.collection('pending_faces').doc(req.params.id).update({ synced: true });
    res.json({ success: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to mark synced' });
  }
});

module.exports = router;
