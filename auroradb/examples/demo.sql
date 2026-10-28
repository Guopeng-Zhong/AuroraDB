-- AuroraDB 教学数据集：姓名和成绩均为虚构。仅用于空数据库；所有修改在一个事务中提交。
BEGIN;
CREATE TABLE students (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    department TEXT NOT NULL,
    year INTEGER NOT NULL,
    gpa REAL
);
CREATE TABLE courses (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    credits INTEGER NOT NULL,
    instructor TEXT NOT NULL
);
CREATE TABLE enrollments (
    id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL,
    course_id INTEGER NOT NULL,
    score REAL
);
INSERT INTO students VALUES
    (1001, '林知远', '计算机科学', 3, 3.92),
    (1002, '陈雨桐', '软件工程', 3, 3.76),
    (1003, '周星河', '计算机科学', 2, 3.85),
    (1004, '许清和', '信息安全', 3, 3.61),
    (1005, '沈嘉宁', '软件工程', 2, 3.94),
    (1006, '陆予安', '计算机科学', 3, 3.48),
    (1007, '苏明夏', '信息安全', 2, 3.88),
    (1008, '顾思齐', '计算机科学', 1, NULL),
    (1009, '江初晴', '软件工程', 3, 3.70),
    (1010, '谢景行', '信息安全', 1, NULL),
    (1011, '唐以墨', '计算机科学', 2, 3.66),
    (1012, '方若溪', '软件工程', 1, 3.82);
INSERT INTO courses VALUES
    (201, '数据库原理', 4, '张老师'),
    (202, '数据结构', 4, '李老师'),
    (203, '操作系统', 3, '王老师'),
    (204, '计算机网络', 3, '赵老师'),
    (205, '软件工程实践', 2, '刘老师');
INSERT INTO enrollments VALUES
    (1, 1001, 201, 96), (2, 1001, 202, 93), (3, 1001, 203, 90),
    (4, 1002, 201, 91), (5, 1002, 205, 95),
    (6, 1003, 201, 94), (7, 1003, 202, 97),
    (8, 1004, 201, 87), (9, 1004, 204, 92),
    (10, 1005, 201, 98), (11, 1005, 205, 96),
    (12, 1006, 201, 82), (13, 1006, 203, 85),
    (14, 1007, 201, 95), (15, 1007, 204, 94),
    (16, 1009, 201, 90), (17, 1009, 205, 92),
    (18, 1011, 202, 89), (19, 1012, 202, NULL);
CREATE INDEX idx_students_department ON students (department);
CREATE INDEX idx_enrollments_student ON enrollments (student_id);
CREATE INDEX idx_enrollments_course ON enrollments (course_id);
CREATE INDEX idx_enrollments_score ON enrollments (score);
COMMIT;
