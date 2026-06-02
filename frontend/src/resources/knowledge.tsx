import {
  Datagrid,
  Edit,
  List,
  SaveButton,
  SimpleForm,
  TextField,
  TextInput,
  Toolbar,
  required,
} from "react-admin";

// 전략 지식 문서 목록 — 자산/정량규격/전략(트랙별)의 활성 버전.
export const KnowledgeList = () => (
  <List perPage={25} title="전략 지식">
    <Datagrid rowClick="edit" bulkActionButtons={false}>
      <TextField source="section" label="문서" />
      <TextField source="track" label="트랙" />
      <TextField source="version" label="버전" />
      <TextField source="author" label="편집자" />
      <TextField source="note" label="메모" />
      <TextField source="created_at" label="수정시각" />
    </Datagrid>
  </List>
);

// 저장만(삭제 없음) — 저장 시 새 버전이 활성화된다.
const EditToolbar = () => (
  <Toolbar>
    <SaveButton label="새 버전으로 저장" />
  </Toolbar>
);

export const KnowledgeEdit = () => (
  <Edit title="전략 지식 편집" mutationMode="pessimistic">
    <SimpleForm toolbar={<EditToolbar />}>
      <TextField source="section" label="문서" />
      <TextField source="track" label="트랙" />
      <TextInput
        source="payloadText"
        label="문서(JSON)"
        multiline
        fullWidth
        minRows={20}
        validate={required()}
        helperText="JSON 형식. 저장하면 새 버전이 활성화됩니다."
      />
      <TextInput
        source="note"
        label="변경 메모"
        fullWidth
        helperText="무엇을·왜 바꿨는지 (감사·롤백용)"
      />
    </SimpleForm>
  </Edit>
);
