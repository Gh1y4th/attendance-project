// middleware/auth.js
//
// Roles: admin > school_admin > professor / family
//  - admin: full access to everything, only role that can create other admins
//    or school_admins
//  - school_admin: full read/write scoped to their own schoolId, can create
//    professor/family accounts within their school
//  - professor: read-only, sees only students where profId == their own id
//  - family: read-only, sees only students where familyIds contains their id
//
// There is NO signup page. Every account is a row in Firestore's `users`
// collection (matched by email) added by an admin or school_admin through
// the dashboard. If your email isn't in there, you can sign in but the API
// will reject you with 403.

const admin = require('firebase-admin');
const { getDb } = require('../firestore');

async function verifyFirebaseToken(req, res, next) {
  const authHeader = req.headers.authorization || '';
  const token = authHeader.startsWith('Bearer ') ? authHeader.slice(7) : null;

  if (!token) {
    return res.status(401).json({ error: 'Missing login token' });
  }

  try {
    const decoded = await admin.auth().verifyIdToken(token);
    req.firebaseUser = decoded;
    next();
  } catch (err) {
    console.error('Invalid Firebase token:', err.message);
    return res.status(401).json({ error: 'Invalid or expired login token' });
  }
}

async function requireDbUser(req, res, next) {
  const email = req.firebaseUser.email;
  const db = getDb();

  const snap = await db.collection('users').where('email', '==', email).limit(1).get();

  if (snap.empty) {
    return res.status(403).json({ error: 'This account is not authorized to access this system' });
  }

  const doc = snap.docs[0];
  const data = doc.data();

  if (data.is_active === false) {
    return res.status(403).json({ error: 'This account has been disabled' });
  }

  req.dbUser = {
    id: doc.id,
    email: data.email,
    role: data.role, // 'admin' | 'school_admin' | 'professor' | 'family'
    full_name: data.full_name,
    schoolId: data.schoolId || null,
    assignedStudentIds: data.assignedStudentIds || [],
    linkedStudentIds: data.linkedStudentIds || [],
  };
  next();
}

function requireRole(...allowedRoles) {
  return (req, res, next) => {
    if (!allowedRoles.includes(req.dbUser.role)) {
      return res.status(403).json({ error: 'You do not have permission to do this' });
    }
    next();
  };
}

function verifyPythonServiceKey(req, res, next) {
  const key = req.headers['x-api-key'];
  if (key !== process.env.PYTHON_SERVICE_API_KEY) {
    return res.status(401).json({ error: 'Invalid API key' });
  }
  next();
}

module.exports = { verifyFirebaseToken, requireDbUser, requireRole, verifyPythonServiceKey };
