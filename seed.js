/**
 * ONE-TIME SCRIPT — run once to create your admin account and a bit of
 * sample data so the dashboard isn't empty on first login.
 *
 * Run from inside backend/:   node seed.js
 */

const admin = require('firebase-admin');
const serviceAccount = require('./firebase-service-account.json');

admin.initializeApp({ credential: admin.credential.cert(serviceAccount) });
const db = admin.firestore();

async function seed() {
  // ---- You, the admin/dev ----
  // If this account already exists in Firebase Auth (e.g. you added it
  // manually in the console), we reuse it instead of trying to create it
  // again. Otherwise we create it with the password below.
  const ADMIN_EMAIL = 'ghiyath2011hk@gmail.com';
  const ADMIN_PASSWORD = '2011201820112018'; // only used if the account doesn't exist yet

  let adminAuthUser;
  try {
    adminAuthUser = await admin.auth().getUserByEmail(ADMIN_EMAIL);
    console.log('Admin account already exists in Firebase Auth, reusing it.');
  } catch (err) {
    if (err.code === 'auth/user-not-found') {
      adminAuthUser = await admin.auth().createUser({ email: ADMIN_EMAIL, password: ADMIN_PASSWORD });
      console.log('Created new admin account in Firebase Auth.');
    } else {
      throw err;
    }
  }

  await db.collection('users').doc(adminAuthUser.uid).set({
    email: ADMIN_EMAIL,
    role: 'admin',
    full_name: 'Ghiyath',
    schoolId: null,
    is_active: true,
    linkedStudentIds: [],
    createdAt: new Date(),
  }, { merge: true });

  // ---- One sample school ----
  const schoolRef = await db.collection('schools').add({
    name: 'Demo School',
    createdAt: new Date(),
  });

  // Sample password for all the demo accounts below.
  const DEMO_PASSWORD = 'DemoPass123!';

  async function getOrCreateAuthUser(email, password) {
    try {
      return await admin.auth().getUserByEmail(email);
    } catch (err) {
      if (err.code === 'auth/user-not-found') {
        return await admin.auth().createUser({ email, password });
      }
      throw err;
    }
  }

  // ---- One sample school_admin ----
  const schoolAdminAuth = await getOrCreateAuthUser('school.admin@example.com', DEMO_PASSWORD);
  await db.collection('users').doc(schoolAdminAuth.uid).set({
    email: 'school.admin@example.com',
    role: 'school_admin',
    full_name: 'Demo School Admin',
    schoolId: schoolRef.id,
    is_active: true,
    linkedStudentIds: [],
    createdAt: new Date(),
  }, { merge: true });

  // ---- One sample professor ----
  const profAuth = await getOrCreateAuthUser('professor@example.com', DEMO_PASSWORD);
  const profRef = db.collection('users').doc(profAuth.uid);
  await profRef.set({
    email: 'professor@example.com',
    role: 'professor',
    full_name: 'Demo Professor',
    schoolId: schoolRef.id,
    is_active: true,
    linkedStudentIds: [],
    createdAt: new Date(),
  }, { merge: true });

  // ---- One sample family ----
  const familyAuth = await getOrCreateAuthUser('family@example.com', DEMO_PASSWORD);
  const familyRef = db.collection('users').doc(familyAuth.uid);
  await familyRef.set({
    email: 'family@example.com',
    role: 'family',
    full_name: 'Demo Family',
    schoolId: schoolRef.id,
    is_active: true,
    linkedStudentIds: [],
    createdAt: new Date(),
  }, { merge: true });

  // ---- Class & subject ----
  const classRef = await db.collection('classes').add({ class_name: 'Grade 10 - A', schoolId: schoolRef.id });
  const subjectRef = await db.collection('subjects').add({ subject_name: 'Mathematics', schoolId: schoolRef.id });

  // ---- Student, assigned to the sample professor and family ----
  const studentRef = await db.collection('students').add({
    full_name: 'Demo Student',
    schoolId: schoolRef.id,
    class_id: classRef.id,
    profId: profRef.id,
    familyIds: [familyRef.id],
    createdAt: new Date(),
  });

  // Link the family to the student both ways
  await familyRef.update({ linkedStudentIds: [studentRef.id] });

  // ---- Course session ----
  const sessionRef = await db.collection('course_sessions').add({
    class_id: classRef.id,
    subject_id: subjectRef.id,
    professor_id: profRef.id,
    day_of_week: 'Sun',
    start_time: '08:00',
    end_time: '08:45',
    room_camera_id: 'CAM-101',
  });

  // ---- Sample attendance record ----
  await db.collection('attendance').add({
    student_id: studentRef.id,
    session_id: sessionRef.id,
    status: 'present',
    confidence_score: 0.98,
    check_in_time: new Date(),
    edited_by: null,
    edited_at: null,
  });

  console.log('Seed complete!');
  console.log('Admin login: ghiyath2011hk@gmail.com / ' + ADMIN_PASSWORD);
  console.log('(Demo school_admin / professor / family accounts all use password: ' + DEMO_PASSWORD + ')');
  process.exit(0);
}

seed().catch((err) => {
  console.error('Seed failed:', err);
  process.exit(1);
});
