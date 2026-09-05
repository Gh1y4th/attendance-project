// routes/attendance.js
const express = require('express');
const router = express.Router();
const { getDb, getDocsByIds } = require('../firestore');
const { verifyFirebaseToken, requireDbUser, verifyPythonServiceKey } = require('../middleware/auth');

async function enrichAttendance(attendanceDocs) {
  const studentIds = attendanceDocs.map((d) => d.student_id);
  const studentsMap = await getDocsByIds('students', studentIds);

  const sessionIds = attendanceDocs.map((d) => d.session_id).filter(Boolean);
  const sessionsMap = await getDocsByIds('course_sessions', sessionIds);

  const subjectIds = Object.values(sessionsMap).map((s) => s.subject_id);
  const subjectsMap = await getDocsByIds('subjects', subjectIds);

  return attendanceDocs.map((a) => {
    const student = studentsMap[a.student_id] || {};
    const session = sessionsMap[a.session_id] || {};
    const subject = subjectsMap[session.subject_id] || {};

    return {
      ATTENDANCE_ID: a.id,
      STUDENT_NAME: student.full_name || 'Unknown',
      SUBJECT_NAME: subject.subject_name || 'Unknown',
      CHECK_IN_TIME: a.check_in_time,
      STATUS: a.status,
      CONFIDENCE_SCORE: a.confidence_score || null,
    };
  });
}

async function fetchByStudentIds(db, studentIds) {
  if (!studentIds || studentIds.length === 0) return [];
  const chunks = [];
  for (let i = 0; i < studentIds.length; i += 30) chunks.push(studentIds.slice(i, i + 30));

  const results = [];
  for (const chunk of chunks) {
    const snap = await db.collection('attendance').where('student_id', 'in', chunk).get();
    results.push(...snap.docs.map((d) => ({ id: d.id, ...d.data() })));
  }
  results.sort((a, b) => (b.check_in_time?._seconds || 0) - (a.check_in_time?._seconds || 0));
  return results;
}

router.get('/', verifyFirebaseToken, requireDbUser, async (req, res) => {
  const db = getDb();
  const { role, id: userId, schoolId, linkedStudentIds } = req.dbUser;

  try {
    let attendanceDocs = [];

    if (role === 'admin') {
      const snap = await db.collection('attendance').orderBy('check_in_time', 'desc').get();
      attendanceDocs = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
    } else if (role === 'school_admin') {
      const studentsSnap = await db.collection('students').where('schoolId', '==', schoolId).get();
      const studentIds = studentsSnap.docs.map((d) => d.id);
      attendanceDocs = await fetchByStudentIds(db, studentIds);
    } else if (role === 'professor') {
      const studentsSnap = await db.collection('students').where('profId', '==', userId).get();
      const studentIds = studentsSnap.docs.map((d) => d.id);
      attendanceDocs = await fetchByStudentIds(db, studentIds);
    } else if (role === 'family') {
      attendanceDocs = await fetchByStudentIds(db, linkedStudentIds);
    }

    const enriched = await enrichAttendance(attendanceDocs);
    res.json(enriched);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to load attendance' });
  }
});

router.patch('/:id', verifyFirebaseToken, requireDbUser, async (req, res) => {
  if (!['admin', 'school_admin'].includes(req.dbUser.role)) {
    return res.status(403).json({ error: 'Only admin or school admin can edit attendance records' });
  }

  const { status } = req.body;
  const validStatuses = ['present', 'late', 'absent', 'excused'];
  if (!validStatuses.includes(status)) {
    return res.status(400).json({ error: 'Invalid status value' });
  }

  const db = getDb();
  try {
    await db.collection('attendance').doc(req.params.id).update({
      status,
      edited_by: req.dbUser.id,
      edited_at: new Date(),
    });
    res.json({ success: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to update record' });
  }
});

router.post('/', verifyPythonServiceKey, async (req, res) => {
  const { student_id, session_id, confidence_score } = req.body;
  if (!student_id) return res.status(400).json({ error: 'student_id is required' });

  const db = getDb();
  try {
    if (session_id) {
      const existing = await db
        .collection('attendance')
        .where('student_id', '==', student_id)
        .where('session_id', '==', session_id)
        .limit(1)
        .get();
      if (!existing.empty) {
        return res.status(200).json({ success: true, message: 'Already logged, skipped' });
      }
    }

    await db.collection('attendance').add({
      student_id,
      session_id: session_id || null,
      status: 'present',
      confidence_score: confidence_score || null,
      check_in_time: new Date(),
      edited_by: null,
      edited_at: null,
    });

    res.status(201).json({ success: true, message: 'Attendance logged' });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to log attendance' });
  }
});

router.get('/current-session', verifyPythonServiceKey, async (req, res) => {
  const { camera_id } = req.query;
  if (!camera_id) return res.status(400).json({ error: 'camera_id is required' });

  const db = getDb();
  const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const now = new Date();
  const today = dayNames[now.getDay()];
  const currentTime = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;

  try {
    const snap = await db
      .collection('course_sessions')
      .where('room_camera_id', '==', camera_id)
      .where('day_of_week', '==', today)
      .get();

    const match = snap.docs.find((d) => {
      const s = d.data();
      return s.start_time <= currentTime && currentTime <= s.end_time;
    });

    if (!match) return res.status(404).json({ error: 'No active session right now for this camera' });
    res.json({ session_id: match.id, ...match.data() });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to find current session' });
  }
});

module.exports = router;
