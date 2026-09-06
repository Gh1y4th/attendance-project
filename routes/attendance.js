const express = require('express');
const router = express.Router();
const { getDb } = require('../firestore');
const { verifyFirebaseToken, requireDbUser, verifyPythonServiceKey } = require('../middleware/auth');

router.get('/', verifyFirebaseToken, requireDbUser, async (req, res) => {
  const db = getDb();
  try {
    const snap = await db.collection('attendance').orderBy('check_in_time', 'desc').get();
    const rows = snap.docs.map((d) => {
      const data = d.data();
      return { ATTENDANCE_ID: d.id, NAME: data.name, CHECK_IN_TIME: data.check_in_time, STATUS: data.status, CONFIDENCE_SCORE: data.confidence_score || null };
    });
    res.json(rows);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to load attendance' });
  }
});

router.patch('/:id', verifyFirebaseToken, requireDbUser, async (req, res) => {
  if (!['dev', 'school_admin'].includes(req.dbUser.role)) {
    return res.status(403).json({ error: 'Only dev or school admin can edit attendance records' });
  }
  const { status } = req.body;
  const validStatuses = ['present', 'late', 'absent', 'excused'];
  if (!validStatuses.includes(status)) return res.status(400).json({ error: 'Invalid status value' });

  const db = getDb();
  try {
    await db.collection('attendance').doc(req.params.id).update({ status, edited_by: req.dbUser.id, edited_at: new Date() });
    res.json({ success: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to update record' });
  }
});

router.post('/', verifyPythonServiceKey, async (req, res) => {
  const { name, confidence_score } = req.body;
  if (!name) return res.status(400).json({ error: 'name is required' });

  const db = getDb();
  try {
    const startOfDay = new Date();
    startOfDay.setHours(0, 0, 0, 0);

    const existing = await db.collection('attendance').where('name', '==', name).where('check_in_time', '>=', startOfDay).limit(1).get();
    if (!existing.empty) return res.status(200).json({ success: true, message: 'Already logged today, skipped' });

    await db.collection('attendance').add({
      name, status: 'present', confidence_score: confidence_score || null,
      check_in_time: new Date(), edited_by: null, edited_at: null,
    });

    res.status(201).json({ success: true, message: 'Attendance logged' });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to log attendance' });
  }
});

module.exports = router;
