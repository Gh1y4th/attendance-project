const express = require('express');
const router = express.Router();
const { getDb } = require('../firestore');
const { verifyFirebaseToken, requireDbUser, verifyPythonServiceKey } = require('../middleware/auth');

function getDayRange(date) {
  const start = new Date(date);
  start.setHours(0, 0, 0, 0);

  const end = new Date(date);
  end.setHours(23, 59, 59, 999);

  return { start, end };
}

router.get('/', verifyFirebaseToken, requireDbUser, async (req, res) => {
  const db = getDb();
  try {
    const snap = await db.collection('attendance').orderBy('check_in_time', 'desc').get();
    const rows = snap.docs.map((d) => {
      const data = d.data();
      return { ATTENDANCE_ID: d.id, STUDENT_ID: data.student_id || null, NAME: data.name, CHECK_IN_TIME: data.check_in_time, STATUS: data.status, CONFIDENCE_SCORE: data.confidence_score || null };
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
  const { status, check_in_time } = req.body;
  const validStatuses = ['present', 'absent'];
  if (!validStatuses.includes(status)) return res.status(400).json({ error: 'Invalid status value' });

  const updates = { status, edited_by: req.dbUser.id, edited_at: new Date() };

  // optional manual time correction - only touches check_in_time if the
  // caller actually sent one, and only if it parses to a real date
  if (check_in_time !== undefined) {
    const parsedTime = new Date(check_in_time);
    if (Number.isNaN(parsedTime.getTime())) {
      return res.status(400).json({ error: 'Invalid check_in_time' });
    }
    updates.check_in_time = parsedTime;
  }

  const db = getDb();
  try {
    await db.collection('attendance').doc(req.params.id).update(updates);
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
    const { start, end } = getDayRange(new Date());

    // IMPORTANT: only one where() is used here on purpose. A compound query
    // like where('name','==',name).where('check_in_time','>=',start) needs a
    // Firestore composite index that doesn't exist, and fails with a silent
    // 500 (FAILED_PRECONDITION) instead of actually logging attendance.
    // Fetch this person's records by name only, then filter by date in JS.
    const existingSnap = await db.collection('attendance').where('name', '==', name).get();

    const alreadyLoggedToday = existingSnap.docs.some((doc) => {
      let t = doc.data().check_in_time;
      if (t && typeof t.toDate === 'function') t = t.toDate();
      else if (t) t = new Date(t);
      return t instanceof Date && !Number.isNaN(t.getTime()) && t >= start && t <= end;
    });

    if (alreadyLoggedToday) {
      return res.status(200).json({ success: true, message: 'Already logged today, skipped' });
    }

    await db.collection('attendance').add({
      name, status: 'present', confidence_score: confidence_score || null,
      check_in_time: new Date(), edited_by: null, edited_at: null,
    });

    res.status(201).json({ success: true, message: 'Attendance logged' });
  } catch (err) {
    console.error('[ATTENDANCE POST ERROR]', err);
    res.status(500).json({ error: 'Failed to log attendance', details: String(err.message || err) });
  }
});

// =========================================================
// POST /api/attendance/manual
//
// Lets a dev or school_admin set a student's status for today
// straight from the dashboard, even if the camera never logged
// that student today (e.g. marking someone "absent" or "excused"
// by hand). Creates today's record if none exists yet, otherwise
// updates the existing one - same duplicate-check approach as the
// camera's POST route above (single where(), date filtering in JS).
// =========================================================
router.post('/manual', verifyFirebaseToken, requireDbUser, async (req, res) => {
  if (!['dev', 'school_admin'].includes(req.dbUser.role)) {
    return res.status(403).json({
      success: false,
      error: 'Only dev or school admin can edit attendance records',
    });
  }

  const { student_id, status, check_in_time } = req.body || {};
  const validStatuses = ['present', 'absent'];

  if (!student_id) {
    return res.status(400).json({ success: false, error: 'student_id is required' });
  }

  if (!validStatuses.includes(status)) {
    return res.status(400).json({ success: false, error: 'Invalid status value' });
  }

  let manualCheckInTime = new Date();
  if (check_in_time !== undefined) {
    const parsedTime = new Date(check_in_time);
    if (Number.isNaN(parsedTime.getTime())) {
      return res.status(400).json({ success: false, error: 'Invalid check_in_time' });
    }
    manualCheckInTime = parsedTime;
  }

  const db = getDb();

  try {
    const studentRef = db.collection('students').doc(student_id);
    const studentSnap = await studentRef.get();

    if (!studentSnap.exists) {
      return res.status(404).json({ success: false, error: 'Student not found' });
    }

    const student = studentSnap.data();
    const studentFullName = String(student.full_name || '').trim();

    const { start, end } = getDayRange(new Date());

    const studentAttendanceSnap = await db
      .collection('attendance')
      .where('student_id', '==', student_id)
      .get();

    let existingDoc = null;

    for (const doc of studentAttendanceSnap.docs) {
      const data = doc.data();
      let existingTime = data.check_in_time;

      if (existingTime && typeof existingTime.toDate === 'function') {
        existingTime = existingTime.toDate();
      } else if (existingTime) {
        existingTime = new Date(existingTime);
      }

      if (
        existingTime instanceof Date &&
        !Number.isNaN(existingTime.getTime()) &&
        existingTime >= start &&
        existingTime <= end
      ) {
        existingDoc = doc;
        break;
      }
    }

    if (existingDoc) {
      await existingDoc.ref.update({
        status,
        check_in_time: manualCheckInTime,
        edited_by: req.dbUser.id,
        edited_at: new Date(),
      });

      return res.json({
        success: true,
        message: 'Attendance updated',
        attendance_id: existingDoc.id,
      });
    }

    const attendanceRef = await db.collection('attendance').add({
      student_id,
      name: studentFullName,
      student_name: studentFullName,
      status,
      confidence_score: null,
      check_in_time: manualCheckInTime,
      edited_by: req.dbUser.id,
      edited_at: new Date(),
      created_at: new Date(),
      manual: true,
    });

    return res.status(201).json({
      success: true,
      message: 'Attendance created',
      attendance_id: attendanceRef.id,
    });
  } catch (err) {
    console.error('[ATTENDANCE MANUAL ERROR]', err);

    return res.status(500).json({
      success: false,
      error: 'Failed to set attendance',
      details: process.env.NODE_ENV === 'production' ? undefined : String(err.message || err),
    });
  }
});

module.exports = router;
