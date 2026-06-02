import { Admin, Resource } from "react-admin";
import LibraryBooksIcon from "@mui/icons-material/LibraryBooks";

import { authProvider } from "./authProvider";
import { dataProvider } from "./dataProvider";
import { KnowledgeEdit, KnowledgeList } from "./resources/knowledge";

const App = () => (
  <Admin
    title="WA 어드민 — 교육 입찰 전략 지식"
    authProvider={authProvider}
    dataProvider={dataProvider}
    requireAuth
  >
    <Resource
      name="knowledge"
      options={{ label: "전략 지식" }}
      icon={LibraryBooksIcon}
      list={KnowledgeList}
      edit={KnowledgeEdit}
    />
  </Admin>
);

export default App;
