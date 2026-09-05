// routes/students.js
const express = require('express');
const router = express.Router();
const { getDb, getDocsByIds } = require('../firestore');
const { verifyFirebaseToken, requireDbUser, requireRole } = require('../middleware/auth');

router.get('/', verifyFirebaseToken, requireDbUser, async (req, res) => {
  const db = getDb();
  const { role, id: userId, schoolId, linkedStudentIds } = req.dbUser;

  try {
    let students = [];

    if (role === 'admin') {
      const snap = await db.collection('students').get();
      students = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
    } else if (role === 'school_admin') {
      const snap = await db.collection('students').where('schoolId', '==', schoolId).get();
      students = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
    } else if (role === 'professor') {
      const snap = await db.collection('students').where('profId', '==', userId).get();
      students = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
    } else if (role === 'family') {
      if (linkedStudentIds.length > 0) {
        const map = await getDocsByIds('students', linkedStudentIds);
        students = Object.entries(map).map(([id, data]) => ({ id, ...data }));
      }
    }

    res.json(students);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to load students' });
  }
});

router.post('/', verifyFirebaseToken, requireDbUser, requireRole('admin', 'school_admin'), async (req, res) => {
  const db = getDb();
  const caller = req.dbUser;
  let { full_name, schoolId, profId, familyIds } = req.body;

  if (!full_name) return res.status(400).json({ error: 'full_name is required' });

  if (caller.role === 'school_admin') {
    schoolId = caller.schoolId;
  } else if (!schoolId) {
    return res.status(400).json({ error: 'schoolId is required' });
  }

  try {
    const docRef = await db.collection('students').add({
      full_name,
      schoolId,
      profId: profId || null,
      familyIds: familyIds || [],
      createdAt: new Date(),
    });
    res.status(201).json({ id: docRef.id, success: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to create student' });
  }
});

router.patch('/:id', verifyFirebaseToken, requireDbUser, requireRole('admin', 'school_admin'), async (req, res) => {
  const db = getDb();
  const caller = req.dbUser;
  const ref = db.collection('students').doc(req.params.id);
  const snap = await ref.get();

  if (!snap.exists) return res.status(404).json({ error: 'Student not found' });
  const student = snap.data();

  if (caller.role === 'school_admin' && student.schoolId !== caller.schoolId) {
    return res.status(403).json({ error: 'You cannot edit this student' });
  }

  const allowedFields = ['full_name', 'profId', 'familyIds'];
  const updates = {};
  for (const field of allowedFields) {
    if (req.body[field] !== undefined) updates[field] = req.body[field];
  }
  updates.updatedAt = new Date();

  try {
    await ref.update(updates);
    res.json({ success: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to update student' });
  }
});

router.get('/lookup', require('../middleware/auth').verifyPythonServiceKey, async (req, res) => {
  const db = getDb();
  const { name } = req.query;
  if (!name) return res.status(400).json({ error: 'name query param is required' });

  try {
    const snap = await db.collection('students').get();
    const match = snap.docs.find(
      (d) => (d.data().full_name || '').trim().toLowerCase() === name.trim().toLowerCase()
    );

    if (!match) return res.status(404).json({ error: 'No student found with that name' });
    res.json({ student_id: match.id, full_name: match.data().full_name });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Lookup failed' });
  }
});

module.exports = router;
